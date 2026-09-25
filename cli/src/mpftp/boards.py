"""Boards mpftp can reach over Wi-Fi, and their WebREPL passwords.

Remembered boards live in ``~/.mpftp/config.json`` under ``wifiBoards``,
keyed by the board's ``machine.unique_id()`` in hex. A serial connection to a
board whose Wi-Fi is up records its address there, so the Wi-Fi connect list
can offer it by name afterwards.

Passwords are kept apart, in ``~/.mpftp/webrepl-passwords.json``, one per
board. The file is created with mode 0600 where the operating system has
POSIX modes. It is plaintext on disk: anyone who can read your home directory
can read it. The VS Code extension doesn't use this file; it keeps passwords in
VS Code's SecretStorage instead.

This module runs on the side of the user's frontend (CLI or PWA server), not
in the sidecar, because a Windows sidecar spawned from WSL has a different
home directory.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from . import ble, config, webrepl

PASSWORDS_NAME = "webrepl-passwords.json"
KEY = config.WIFI_BOARDS_KEY


def _passwords_path() -> Path:
    return config.CONFIG_DIR / PASSWORDS_NAME


# --------------------------------------------------------------------------
# Remembered boards
# --------------------------------------------------------------------------


def load() -> dict[str, dict[str, Any]]:
    """``{uid: {"name", "ip", "hostname", "port", "machine", "seen"}}``."""
    try:
        data = config.validate(config.read_file()).get(KEY) or {}
    except config.ConfigError:
        return {}
    return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}


def remember(identity: dict[str, Any], *, address: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Record what a connect found. Returns the stored entry, or None.

    ``identity`` is the sidecar's ``board`` result. Over serial it carries the
    board's ``ip`` when Wi-Fi is up. Over Wi-Fi ``address`` is the ws:// device
    that just worked, so a board reached by a typed address is remembered too.
    """
    uid = str(identity.get("uid") or "").strip().lower()
    if not uid:
        return None
    ip = identity.get("ip")
    port = webrepl.DEFAULT_PORT
    if address and webrepl.is_network_device(address):
        host, port, _path = webrepl.parse_device(address)
        ip = ip or host
    if not ip:
        return None
    boards = load()
    entry = dict(boards.get(uid) or {})
    entry.update(
        {
            "name": entry.get("name") or _name(identity),
            "ip": ip,
            "port": port,
            "seen": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
    )
    for field in ("hostname", "machine"):
        if identity.get(field):
            entry[field] = identity[field]
    boards[uid] = entry
    try:
        config.update({KEY: boards})
    except config.ConfigError:
        return None
    return entry


def _name(identity: dict[str, Any]) -> str:
    return str(identity.get("hostname") or identity.get("uid") or "board")


def forget(uid: str) -> bool:
    boards = load()
    if uid not in boards:
        return False
    del boards[uid]
    config.update({KEY: boards})
    return True


def device_for(entry: dict[str, Any]) -> str:
    port = int(entry.get("port") or webrepl.DEFAULT_PORT)
    return f"ws://{entry['ip']}" + ("" if port == webrepl.DEFAULT_PORT else f":{port}")


def uid_for_device(device: str) -> Optional[str]:
    """The remembered board at this ws:// address (by IP or hostname)."""
    if not webrepl.is_network_device(device):
        return None
    try:
        host, _port, _path = webrepl.parse_device(device)
    except ValueError:
        return None
    host = host.lower()
    short = host[:-6] if host.endswith(".local") else host
    for uid, entry in load().items():
        if host == str(entry.get("ip") or "").lower():
            return uid
        if short and short == str(entry.get("hostname") or "").lower():
            return uid
        if short == str(entry.get("name") or "").lower():
            return uid
    return None


def listing() -> list[dict[str, Any]]:
    """Remembered boards for a picker, most recently seen first."""
    stored = passwords()
    rows = []
    for uid, entry in load().items():
        rows.append(
            {
                "uid": uid,
                "name": entry.get("name") or uid,
                "ip": entry.get("ip"),
                "hostname": entry.get("hostname"),
                "device": device_for(entry),
                "seen": entry.get("seen"),
                "hasPassword": uid in stored,
            }
        )
    rows.sort(key=lambda r: str(r.get("seen") or ""), reverse=True)
    return rows


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------


def password_key(device_or_uid: str) -> str:
    """A board's key in the password file: its uid, else ``host:<address>``,
    or ``ble:<name>`` for a board reached over Bluetooth."""
    if ble.is_ble_device(device_or_uid):
        return "ble:" + ble.parse_device(device_or_uid).lower()
    if webrepl.is_network_device(device_or_uid):
        uid = uid_for_device(device_or_uid)
        if uid:
            return uid
        host, port, _path = webrepl.parse_device(device_or_uid)
        return f"host:{host.lower()}:{port}"
    return device_or_uid.strip().lower()


def passwords() -> dict[str, str]:
    try:
        data = json.loads(_passwords_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _write_passwords(data: dict[str, str]) -> None:
    path = _passwords_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".webrepl-", suffix=".json")
    try:
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            pass  # Windows: no POSIX modes; the file inherits the profile's ACL
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_password(device_or_uid: str) -> Optional[str]:
    """This board's password, else the phase-1 single ``webreplPassword``."""
    stored = passwords()
    key = password_key(device_or_uid)
    if key in stored:
        return stored[key]
    if ble.is_ble_device(device_or_uid):
        return config.resolve("blePassword") or config.resolve("webreplPassword") or None
    if webrepl.is_network_device(device_or_uid):
        host, port, _path = webrepl.parse_device(device_or_uid)
        by_host = stored.get(f"host:{host.lower()}:{port}")
        if by_host:
            return by_host
    return config.resolve("webreplPassword") or None


def set_password(device_or_uid: str, password: str) -> str:
    """Store a board's password. Returns the key it went under."""
    if ble.is_ble_device(device_or_uid):
        ble.check_password(password, device_or_uid)
    else:
        webrepl.check_password(password, device_or_uid)
    data = passwords()
    key = password_key(device_or_uid)
    data[key] = password
    _write_passwords(data)
    return key


def adopt_password(device: str, uid: str) -> None:
    """After a connect by typed address, move its password under the board's uid."""
    if not uid or not webrepl.is_network_device(device):
        return
    data = passwords()
    host, port, _path = webrepl.parse_device(device)
    host_key = f"host:{host.lower()}:{port}"
    if host_key in data and uid not in data:
        data[uid] = data.pop(host_key)
        _write_passwords(data)


def forget_password(device_or_uid: str) -> bool:
    data = passwords()
    key = password_key(device_or_uid)
    if key not in data:
        return False
    del data[key]
    _write_passwords(data)
    return True


def record_connect(device: str, result: Any, password: Optional[str] = None) -> None:
    """What every frontend does after a successful connect: remember the board,
    and keep a password that just worked under the board's uid."""
    if not isinstance(result, dict):
        return
    identity = result.get("board")
    if not isinstance(identity, dict):
        return
    network = webrepl.is_network_device(device)
    try:
        remember(identity, address=device if network else None)
        uid = str(identity.get("uid") or "").lower()
        if network and uid:
            adopt_password(device, uid)
            if password and passwords().get(uid) != password:
                set_password(uid, password)
    except (OSError, ValueError, config.ConfigError):
        pass
