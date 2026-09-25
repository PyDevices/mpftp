#!/usr/bin/env python3
"""
mpftp sidecar — long-lived mpremote session for VS Code / Cursor.

Speaks newline-delimited JSON on stdin/stdout.
Uses the official mpremote package (same backend as mpremote.exe).
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

from . import ble, config, mdns, rawpaste, webrepl, wifiboard


def split_fs_path(path: str) -> tuple[bool, str]:
    """Return (is_remote, path). Board paths use mpremote ':' prefix."""
    if path.startswith(":"):
        return True, path[1:]
    return False, path


def host_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def should_skip_transfer_entry(name: str) -> bool:
    """Skip VCS/env/bytecode noise: .git, .venv, __pycache__, *.pyc, etc."""
    if not name or name in (".", ".."):
        return True
    if name.startswith("."):
        return True
    if name == "__pycache__":
        return True
    lower = name.lower()
    return lower.endswith((".pyc", ".pyo"))


def _replace_remote_basename(remote_path: str, new_name: str) -> str:
    """Swap the basename of a mpremote-style ('/'-separated) remote path."""
    if "/" in remote_path:
        return remote_path.rsplit("/", 1)[0] + "/" + new_name
    return new_name


_ELF_MAGIC = b"\x7fELF"


def _mpy_cross_runnable(path: Path) -> bool:
    """True if this process can execute ``path`` (skip Linux ELF under Windows)."""
    if not path.is_file():
        return False
    if sys.platform == "win32":
        try:
            with open(path, "rb") as f:
                magic = f.read(4)
        except OSError:
            return False
        if magic == _ELF_MAGIC:
            return False
    return True


def find_mpy_cross(micropython_hint: Optional[str] = None, workspace: Optional[str] = None) -> str:
    """Resolve mpy-cross, then mpy-cross.exe: firmware-workspace build -> PATH."""
    from .firmware import find_micropython

    mp = find_micropython(micropython_hint, workspace)
    for name in ("mpy-cross", "mpy-cross.exe"):
        if mp is not None:
            candidate = mp / "mpy-cross" / "build" / name
            if _mpy_cross_runnable(candidate):
                return str(candidate)
        found = shutil.which(name)
        if found and _mpy_cross_runnable(Path(found)):
            return found
    raise RuntimeError(
        "mpy-cross not found. Build it in your MicroPython tree (make -C mpy-cross), "
        "`pip install mpy-cross`, or point mpftp.workspacePath / mpftp.micropythonPath "
        "at your tree."
    )


_MPY_CROSS_VERSION_RE = re.compile(r"mpy-cross emitting mpy v(\d+)\.(\d+)")


def mpy_cross_version(exe: str) -> tuple[int, int]:
    """Parse the (version, sub_version) that ``exe --version`` reports it emits."""
    proc = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
    text = (proc.stdout or "") + (proc.stderr or "")
    m = _MPY_CROSS_VERSION_RE.search(text)
    if not m:
        raise RuntimeError(f"could not determine mpy-cross version from: {text.strip() or '(no output)'}")
    return int(m.group(1)), int(m.group(2))


def map_circuitpy_remote_path(host_root: Path, remote: str) -> Path:
    """Map a board path (``/lib/foo.py``) onto a host CIRCUITPY root."""
    rel = (remote or "").strip().replace("\\", "/")
    if rel in ("", "/"):
        return host_root
    parts = [p for p in rel.lstrip("/").split("/") if p and p != ".."]
    return host_root.joinpath(*parts) if parts else host_root


def normalize_interpreter_name(name: Any) -> str:
    """Map ``sys.implementation.name`` to ``micropython`` | ``circuitpython``."""
    n = str(name or "").strip().lower()
    if n in ("circuitpython", "circuit"):
        return "circuitpython"
    return "micropython"


# CircuitPython "Press any key…" banners (Thonny cirpy_back._ENTER_REPL_PHRASES).
# Substring match — localization variants share distinctive fragments.
_CP_ENTER_REPL_MARKERS = (
    b"Press any key to enter the REPL",
    b"Appuyez sur n'importe quelle touche pour utiliser le REPL",
    b"Presiona cualquier tecla para entrar al REPL",
    b"Dr\xc3\xbccke eine beliebige Taste um REPL",
    b"Druk een willekeurige toets om de REPL",
    b"Tekan sembarang tombol untuk masuk ke REPL",
    b"Pressione qualquer tecla para entrar no REPL",
    b"Tryck p\xc3\xa5 valfri tangent f\xc3\xb6r att g\xc3\xa5 in i REPL",
    "Нажмите любую клавишу чтобы зайти в REPL".encode("utf-8"),
)


def data_has_enter_repl_prompt(data: bytes) -> bool:
    """True if UART output is waiting for a key to enter the friendly REPL."""
    if not data:
        return False
    return any(m in data for m in _CP_ENTER_REPL_MARKERS)


def is_dead_serial_error(exc: BaseException) -> bool:
    """True when the Windows/pyserial handle is unusable (Access denied, etc.)."""
    msg = str(exc).lower()
    needles = (
        "access is denied",
        "permissionerror",
        "clearcommerror",
        "getoverlappedresult",
        "writefile failed",
        "failed to access",
        "device not configured",
        "port is closed",
        "working outside of",  # closed handle edge cases
        "write timeout",  # SerialTimeoutException: wedged handle can't drain (mpftp#2)
    )
    return any(n in msg for n in needles)


# pyserial defaults to write_timeout=None (block forever). A COM handle left
# over from a killed/SIGTERM'd process can wedge WriteFile indefinitely with
# no exception raised at all — set a finite bound so a hung connect/interrupt
# fails fast instead of hanging the sidecar (and every RPC queued behind it,
# mpftp#2) forever.
WRITE_TIMEOUT_SECS = 8.0


def _bound_write_timeout(transport: Any, secs: float = WRITE_TIMEOUT_SECS) -> None:
    serial = getattr(transport, "serial", None)
    if serial is not None:
        try:
            serial.write_timeout = secs
        except Exception:
            pass


def is_eof_timeout_error(exc: BaseException) -> bool:
    """True when mpremote follow-exec timed out waiting for raw-REPL EOF."""
    msg = str(exc).lower()
    return "timeout waiting for first eof" in msg or (
        "timeout waiting for" in msg and "eof" in msg
    )


def friendly_exec_timeout_message(detail: str) -> str:
    return (
        f"{detail}\n"
        "The board is still running (no raw-REPL EOF) or the serial handle wedged. "
        "For UI apps / loops use --no-follow (or run_path follow=false). "
        "To watch one that is already running, use `mpftp monitor` -- it is "
        "read-only, so the program keeps going; every other command takes the "
        "raw REPL and stops it. "
        "To stop it: interrupt, soft-reset, or hard-reset. "
        "mpftp released the COM handle so Connect/Resume can reclaim the port."
    )


class FollowTimeoutError(RuntimeError):
    """Follow-exec timed out; ``partial_output`` is what the board printed first.

    mpremote's ``follow`` raises a bare TransportError on its quiet timeout and
    drops everything the script printed, so a program that hung after twenty
    lines looked identical to one that never started (mpftp#25).
    """

    def __init__(self, message: str, partial_output: str = "") -> None:
        super().__init__(message)
        self.partial_output = partial_output


def exec_follow(transport: Any, source: str) -> str:
    """``transport.exec(source)`` that keeps the output when the follow times out.

    Streams the board's bytes through mpremote's ``data_consumer`` hook rather
    than letting ``follow`` accumulate them in a local its timeout discards; a
    timeout then raises :class:`FollowTimeoutError` carrying the partial text.
    """
    chunks: list[bytes] = []
    try:
        transport.exec(source, data_consumer=chunks.append)
    except Exception as e:
        if is_eof_timeout_error(e):
            raise FollowTimeoutError(str(e), _consumed_text(chunks)) from e
        raise
    return _consumed_text(chunks)


def _consumed_text(chunks: list[bytes]) -> str:
    data = b"".join(chunks)
    # With a data_consumer mpremote hands over the raw-REPL EOF byte too.
    if data.endswith(b"\x04"):
        data = data[:-1]
    return data.decode("utf-8", "replace")


def annotate_port_roles(ports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add suggested ``role`` for dual-USB boards (UART bridge vs native CDC)."""
    has_espressif = any(p.get("vid") == 0x303A for p in ports)
    has_wch = any(p.get("vid") == 0x1A86 for p in ports)
    for p in ports:
        if not p.get("repl", True):
            p["role"] = "data"
            continue
        vid = p.get("vid")
        if has_espressif and has_wch:
            if vid == 0x303A:
                p["role"] = "cdc_debug"
            elif vid == 0x1A86:
                p["role"] = "repl"
            else:
                p["role"] = "serial"
        elif vid == 0x303A:
            p["role"] = "usb_cdc"
        elif vid == 0x1A86:
            p["role"] = "uart_bridge"
        else:
            p["role"] = "serial"
    return ports


# USB-UART bridges: WCH (CH34x), Silicon Labs (CP210x), FTDI, Prolific. On an
# ESP32 their DTR/RTS lines drive EN and IO0.
_UART_BRIDGE_VIDS = frozenset({0x1A86, 0x10C4, 0x0403, 0x067B})
# Espressif's USB-Serial-JTAG: its DTR/RTS reset the chip too.
_ESP_USB_SERIAL_JTAG = (0x303A, 0x1001)
# Vendors whose CDC ports are TinyUSB running on the chip itself.
_NATIVE_CDC_VIDS = frozenset({0x303A, 0x239A, 0x2E8A})


def console_wants_dtr(vid: Any, pid: Any, interpreter: Optional[str]) -> bool:
    """Should a read-only capture of this port raise DTR? (mpftp#60)

    A TinyUSB CDC port (CircuitPython's USB, and MicroPython's on rp2 and on
    esp32 boards built with TinyUSB) treats DTR as "a host is listening" and
    sends nothing while it's low; MicroPython buffers and CircuitPython
    discards. Nothing is wired to reset there, so raising it is safe. On a
    USB-UART bridge or the ESP32 USB-Serial-JTAG, DTR and RTS reset the chip
    or pick its boot mode, so both stay low, as ``monitor`` keeps them.
    """
    if not isinstance(vid, int):
        return False
    if vid in _UART_BRIDGE_VIDS or (vid, pid) == _ESP_USB_SERIAL_JTAG:
        return False
    if vid in _NATIVE_CDC_VIDS:
        return True
    # CircuitPython's own USB is always TinyUSB, whatever vendor ID it wears.
    return (interpreter or "").lower() == "circuitpython"


def circup_boot_out_text(*, cpy_version: str, board_id: str = "unknown") -> str:
    """Minimal boot_out.txt so circup --path can resolve board/version."""
    ver = (cpy_version or "9.0.0").strip()
    bid = (board_id or "unknown").strip() or "unknown"
    return (
        f"Adafruit CircuitPython {ver} on 2020-01-01; "
        f"{bid} with unknown\n"
        f"Board ID:{bid}\n"
    )


def build_circup_argv(
    *,
    circup_exe: str,
    stage_path: str,
    packages: list[str],
    cpy_version: str,
    board_id: str = "unknown",
    py: bool = False,
) -> list[str]:
    """CLI argv for host-side circup install into a staging directory."""
    argv = [
        circup_exe,
        "--path",
        stage_path,
        "--board-id",
        board_id or "unknown",
        "--cpy-version",
        cpy_version or "9.0.0",
        "install",
    ]
    if py:
        argv.append("--py")
    argv.extend(packages)
    return argv


def build_circup_web_argv(
    *,
    circup_exe: str,
    host: str,
    password: str,
    packages: list[str],
    py: bool = False,
) -> list[str]:
    """CLI argv for circup install over CircuitPython Web Workflow."""
    argv = [
        circup_exe,
        "--host",
        host,
        "--password",
        password,
        "install",
    ]
    if py:
        argv.append("--py")
    argv.extend(packages)
    return argv


# Ensure UTF-8 stdio on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")


def _emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _notify(method: str, params: Optional[dict[str, Any]] = None) -> None:
    _emit({"type": "notify", "method": method, "params": params or {}})


def _result(req_id: Any, result: Any = None) -> None:
    _emit({"type": "result", "id": req_id, "result": result})


def _error(
    req_id: Any, message: str, data: Any = None, partial_output: Optional[str] = None
) -> None:
    err: dict[str, Any] = {"type": "error", "id": req_id, "error": message}
    if data is not None:
        err["data"] = data
    if partial_output is not None:
        err["partialOutput"] = partial_output
    _emit(err)


def _error_from_exception(req_id: Any, exc: BaseException) -> None:
    """Error reply for an exception a METHODS handler raised. Call it from the
    ``except`` block so the traceback is still the current one."""
    _error(
        req_id,
        str(exc),
        traceback.format_exc(),
        partial_output=getattr(exc, "partial_output", None),
    )


def _mpftp_dir() -> Path:
    d = Path.home() / ".mpftp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sanitize_session_id(raw: str) -> str:
    """Filesystem-safe session id for ``~/.mpftp/sessions/<id>.pid``."""
    s = "".join(c if c.isalnum() or c in "._-" else "_" for c in (raw or "").strip())
    s = s.strip("._-") or "session"
    return s[:120]


def resolve_session_id() -> str:
    """Session id from env (extension) or a standalone one-shot id."""
    env = (os.environ.get("MPFTP_SESSION_ID") or "").strip()
    if env:
        return sanitize_session_id(env)
    return sanitize_session_id(f"standalone-{os.getpid()}")


def _sessions_dir() -> Path:
    d = _mpftp_dir() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _legacy_sidecar_pid_path() -> Path:
    return _mpftp_dir() / "sidecar.pid"


def session_pid_path(session_id: Optional[str] = None) -> Path:
    sid = sanitize_session_id(session_id or resolve_session_id())
    return _sessions_dir() / f"{sid}.pid"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _force_kill_pid(pid: int) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    if sys.platform == "win32":
        import subprocess

        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            check=False,
            capture_output=True,
        )
        return
    try:
        os.kill(pid, 9)
    except OSError:
        pass


def _read_pid_file(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def reap_dead_session_pid_files() -> list[str]:
    """Unlink ``sessions/*.pid`` whose process is gone (never kill live peers)."""
    removed: list[str] = []
    try:
        entries = list(_sessions_dir().glob("*.pid"))
    except OSError:
        return removed
    for path in entries:
        pid = _read_pid_file(path)
        if pid and _pid_alive(pid):
            continue
        try:
            path.unlink()
            removed.append(path.name)
        except OSError:
            pass
    return removed


def migrate_legacy_sidecar_pid() -> list[int]:
    """One-shot: kill legacy ``~/.mpftp/sidecar.pid`` if still live, then delete it."""
    killed: list[int] = []
    path = _legacy_sidecar_pid_path()
    if not path.exists():
        return killed
    prev = _read_pid_file(path)
    if prev and prev != os.getpid() and _pid_alive(prev):
        _force_kill_pid(prev)
        killed.append(prev)
    try:
        path.unlink()
    except OSError:
        pass
    return killed


def cleanup_stale_sidecars(session_id: Optional[str] = None) -> list[int]:
    """Kill only this session's prior sidecar; never mass-kill other windows.

    1. Same-session pid file → kill live orphan (window reload).
    2. Reap dead ``sessions/*.pid`` entries (no kill of live foreign sessions).
    3. Migrate legacy global ``sidecar.pid`` once.
    """
    killed: list[int] = []
    sid = sanitize_session_id(session_id or resolve_session_id())
    pid_path = session_pid_path(sid)
    prev = _read_pid_file(pid_path)
    if prev and prev != os.getpid() and _pid_alive(prev):
        _force_kill_pid(prev)
        killed.append(prev)
    try:
        if pid_path.exists():
            pid_path.unlink()
    except OSError:
        pass
    reap_dead_session_pid_files()
    killed.extend(migrate_legacy_sidecar_pid())
    return killed


def claim_sidecar_pid(session_id: Optional[str] = None) -> Path:
    path = session_pid_path(session_id)
    path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    return path


def release_sidecar_pid(session_id: Optional[str] = None) -> None:
    path = session_pid_path(session_id)
    try:
        cur = _read_pid_file(path)
    except Exception:
        return
    if cur == os.getpid():
        try:
            path.unlink()
        except OSError:
            pass


# MicroPython inisetup / _boot spam when the vfs partition is corrupt.
_FS_CORRUPT_MARKERS = (
    b"filesystem appears to be corrupted",
    b"factory reprogramming of MicroPython",
    b"fs_corrupted",
)


class Session:
    #: Pauses before retrying a WebREPL login the board hung up on, because the
    #: client before us may still be closing (see ``_open_transport``).
    WEBREPL_BUSY_RETRIES = (0.3, 0.7)

    def __init__(self) -> None:
        self.transport = None
        self.device: Optional[str] = None
        self.last_device: Optional[str] = None
        self.baud = 115200
        self._lock = threading.RLock()
        self._repl_mode = False
        self._repl_stop = threading.Event()
        self._repl_thread: Optional[threading.Thread] = None
        self._mounted_path: Optional[str] = None
        # "micropython" | "circuitpython" — set on successful probe/connect.
        self.interpreter: Optional[str] = None
        # Set when connect had to skip raw soft-reset because boot loops on a
        # corrupt filesystem (soft-reset would never re-enter raw REPL).
        self.filesystem_warning: Optional[str] = None
        # Optional read-only second COM for debug prints (UART stays control).
        self._tee_stop = threading.Event()
        self._tee_thread: Optional[threading.Thread] = None
        self._tee_serial: Any = None
        self._tee_device: Optional[str] = None
        self._tee_started = 0.0
        # WebREPL password for a ws:// device, held in memory for reconnects.
        self._webrepl_password: Optional[str] = None
        # ws:// devices that completed a login in this process: if one stops
        # answering, the board is alive but busy (see _network_open_error).
        self._answered: set[str] = set()
        # Identity the last connect read from the board (uid, ip, hostname).
        self.board_identity: Optional[dict[str, Any]] = None
        # Wi-Fi power save held off for a transfer: nesting depth, and the
        # value read before it was changed (None when nothing was changed).
        self._pm_depth = 0
        self._pm_saved: Optional[int] = None
        self._pm_report: Optional[dict[str, Any]] = None
        # A value that couldn't be put back (the connection dropped). The next
        # transfer restores this one, not the "off" it would read.
        self._pm_orphan: Optional[int] = None

    def list_ports(self) -> list[dict[str, Any]]:
        import serial.tools.list_ports

        ports = []
        for p in serial.tools.list_ports.comports():
            iface = getattr(p, "interface", None) or ""
            # CircuitPython CDC2 is the data interface, not the REPL.
            repl = "CircuitPython CDC2" not in iface
            ports.append(
                {
                    "device": p.device,
                    "serial_number": p.serial_number,
                    "vid": p.vid if isinstance(p.vid, int) else None,
                    "pid": p.pid if isinstance(p.pid, int) else None,
                    "manufacturer": p.manufacturer,
                    "product": p.product,
                    "description": p.description,
                    "interface": iface or None,
                    "hwid": getattr(p, "hwid", None) or None,
                    "repl": repl,
                }
            )
        annotate_port_roles(ports)
        # Prefer suggested REPL role, then other REPL-capable, CDC2/data last.
        def _port_key(x: dict[str, Any]) -> tuple:
            role = x.get("role") or ""
            if role == "repl":
                pri = 0
            elif x.get("repl", True) and role != "data":
                pri = 1
            else:
                pri = 2
            return (pri, x["device"] or "")

        ports.sort(key=_port_key)
        return ports

    @property
    def is_network(self) -> bool:
        """True when the session's board is reached over WebREPL (ws://)."""
        return webrepl.is_network_device(self.device or self.last_device)

    @property
    def is_ble(self) -> bool:
        """True when the session's board is reached over bledev.repl (ble://)."""
        return ble.is_ble_device(self.device or self.last_device)

    @property
    def is_remote(self) -> bool:
        """Over the air (Wi-Fi or BLE): no serial port, no modem lines, and a
        soft reset ends the link."""
        return self.is_network or self.is_ble

    def _open_transport(self, device: str, baud: int) -> Any:
        """mpremote transport for ``device``: a serial port, ws:// WebREPL or ble://."""
        import time

        if ble.is_ble_device(device):
            password = self._webrepl_password or config.resolve("blePassword") or config.resolve(
                "webreplPassword"
            )
            return ble.open_transport(device, password)
        if webrepl.is_network_device(device):
            password = self._webrepl_password or config.resolve("webreplPassword")
            # WebREPL takes one client and hangs up on a second before the
            # password prompt. The one before us may still be closing (its
            # FIN in flight over Wi-Fi), so give it a moment before saying so.
            for wait in self.WEBREPL_BUSY_RETRIES:
                try:
                    return webrepl.open_transport(device, password)
                except webrepl.WebReplError as e:
                    if "before the password prompt" not in str(e):
                        raise
                time.sleep(wait)
            return webrepl.open_transport(device, password)
        # Raw-paste one window at a time on CircuitPython, whose ESP32 native
        # USB can garble a paste that runs ahead of it (rawpaste.py, mpftp#50).
        return rawpaste.open_serial_transport(
            device, baud, paced=lambda: (self.interpreter or "") == "circuitpython"
        )

    def connect(
        self,
        device: str,
        baud: int = 115200,
        attempts: int = 3,
        password: Optional[str] = None,
        known: bool = False,
    ) -> dict[str, Any]:
        import time

        from mpremote.transport import TransportError

        network = webrepl.is_network_device(device) or ble.is_ble_device(device)
        if network:
            try:
                if ble.is_ble_device(device):
                    ble.parse_device(device)
                else:
                    webrepl.parse_device(device)
            except ValueError as e:
                raise RuntimeError(str(e)) from e
            if password:
                self._webrepl_password = password
            if known:
                self._answered.add(device)

        # The raw-REPL handshake (and sometimes opening the port itself) can fail
        # transiently right after the board enumerates or if the REPL is momentarily
        # busy — the classic "could not enter raw repl". A fresh reopen + reprobe
        # almost always succeeds, so retry a few times before surfacing the error
        # (mirrors what the user would do by clicking connect again).
        retry_delay = 0.4

        with self._lock:
            self.disconnect()
            self.baud = baud
            self.filesystem_warning = None
            last_probe_err: Optional[Exception] = None
            for attempt in range(1, max(1, attempts) + 1):
                last = attempt >= max(1, attempts)
                # (Re)open the serial transport for this attempt.
                try:
                    self.transport = self._open_transport(device, baud)
                    _bound_write_timeout(self.transport)
                except (webrepl.WebReplAuthError, ble.BleAuthError) as e:
                    # Retrying a wrong password only repeats the refusal.
                    raise RuntimeError(str(e)) from e
                except ble.BleError as e:
                    # Already scanned for the board; say what it said.
                    raise RuntimeError(str(e)) from e
                except webrepl.WebReplError as e:
                    # Already waited out its own timeout; a retry triples it.
                    raise RuntimeError(self._network_open_error(device, e)) from e
                except TransportError as e:
                    if not last:
                        time.sleep(retry_delay)
                        continue
                    raise RuntimeError(self._friendly_port_open_error(device, e)) from e
                except OSError as e:
                    if not last:
                        time.sleep(retry_delay)
                        continue
                    raise RuntimeError(self._friendly_port_open_error(device, e)) from e
                # Opening the COM port succeeds in UF2/bootloader mode too — require a
                # MicroPython raw-REPL handshake before we report connected.
                try:
                    rtc = self._probe_micropython(self.transport)
                except Exception as e:
                    try:
                        self.transport.close()
                    except Exception:
                        pass
                    self.transport = None
                    last_probe_err = e
                    if network:
                        # Logged in, then couldn't interrupt: a loop that never
                        # yields. Retrying only waits the same timeout again.
                        self.device = None
                        raise RuntimeError(f"{device}: {wifiboard.STUCK_MESSAGE}") from e
                    if not last:
                        time.sleep(retry_delay)
                        continue
                    self.device = None
                    raise RuntimeError(
                        self._friendly_probe_error(device, e)
                    ) from e
                self.device = device
                self.last_device = device
                if network:
                    self._answered.add(device)
                return self._connect_result(device, baud, rtc, retries=attempt - 1)
            # Unreachable: the loop always returns or raises on the last attempt.
            raise RuntimeError(
                f"{device} could not be connected: {last_probe_err}"
            )

    def _connect_result(
        self,
        device: str,
        baud: int,
        rtc: Optional[list[int]],
        *,
        retries: int = 0,
        resumed: bool = False,
    ) -> dict[str, Any]:
        interpreter = self.interpreter or "micropython"
        result: dict[str, Any] = {
            "device": device,
            "baud": baud,
            "interpreter": interpreter,
            # Legacy bool kept for older UI/agents.
            "micropython": interpreter == "micropython",
        }
        if rtc is not None:
            result["rtc"] = rtc
        if retries > 0:
            result["retries"] = retries
        if resumed:
            result["resumed"] = True
        if self.filesystem_warning:
            result["filesystem_warning"] = self.filesystem_warning
        if self.board_identity:
            result["board"] = dict(self.board_identity)
        return result

    def _network_open_error(self, device: str, exc: BaseException) -> str:
        """What to say when a ws:// device doesn't answer.

        A board spinning in a loop that never yields still accepts TCP and
        answers ping (lwIP has its own task), but WebREPL never replies, since
        the esp32 port only services its sockets from the VM's poll hook. So a
        silent WebREPL on a host that answers ping, or on a board that logged
        in earlier in this session, is a busy board, and the user is told so.
        """
        text = str(exc)
        if isinstance(exc, webrepl.WebReplAuthError):
            return text
        silent = (
            "no answer from" in text
            or "never answered the WebSocket upgrade" in text
            or "no the password prompt" in text
            or "no the login reply" in text
        )
        if not silent:
            return text
        if "never answered the WebSocket upgrade" in text or device in self._answered:
            return f"{device}: {wifiboard.STUCK_MESSAGE}"
        try:
            host = webrepl.parse_device(device)[0]
        except ValueError:
            return text
        if webrepl.host_answers_ping(host):
            return f"{device}: {wifiboard.STUCK_MESSAGE}"
        return text

    def resume(self, baud: Optional[int] = None) -> dict[str, Any]:
        """Reconnect to the last device without requiring the caller to re-pick a port."""
        device = self.device or self.last_device
        if not device:
            raise RuntimeError("no previous device to resume")
        if self.transport and self.device == device:
            # Already connected — re-probe / refresh RTC like a fresh connect would.
            with self._lock:
                try:
                    rtc = self._probe_micropython(self.transport)
                except Exception as e:
                    raise RuntimeError(
                        f"resume failed (still connected but board not responding): {e}"
                    ) from e
                return self._connect_result(
                    device, self.baud, rtc, resumed=True
                )
        return self.connect(device, baud if baud is not None else self.baud)

    def _host_rtc_tuple(self) -> tuple[int, ...]:
        """The host's clock in UTC, as machine.RTC().datetime() takes it.

        UTC, not local time (mpftp#58). MicroPython's RTC holds UTC by
        convention: ntptime.settime() writes UTC and the C code turns it into
        Unix time. CircuitPython has no time zones, but adafruit_ntp defaults
        to tz_offset=0, so an unconfigured board that syncs NTP holds UTC too.
        mpremote's ``rtc --set`` writes local time instead; mpftp does not.
        """
        import time

        tnow = time.gmtime()
        # MicroPython RTC: (year, month, day, weekday, hour, minute, second, subsecond)
        # weekday: Monday=0 (matches time.struct_time.tm_wday)
        return (
            tnow.tm_year,
            tnow.tm_mon,
            tnow.tm_mday,
            tnow.tm_wday,
            tnow.tm_hour,
            tnow.tm_min,
            tnow.tm_sec,
            0,
        )

    # A board clock reading this year or later has been set by something (NTP,
    # a battery-backed RTC, an earlier mpftp), so a connect leaves it alone.
    # An unset clock reads 2000 (MicroPython's and CircuitPython's embedded
    # epoch) or 1970, far below it.
    RTC_SET_YEAR = 2024

    # Board side of _apply_rtc. MicroPython's machine.RTC().datetime is a
    # method taking an 8-tuple; CircuitPython's rtc.RTC().datetime is a
    # property taking a struct_time (y, mo, d, h, mi, s, wday, yday, isdst).
    _RTC_SET_CODE = (
        "def _mpftp_rtc_set(t, force, floor):\n"
        " import time\n"
        " if not force and time.localtime()[0] >= floor:\n"
        "  return None\n"
        " try:\n"
        "  import machine\n"
        " except ImportError:\n"
        "  import rtc\n"
        "  rtc.RTC().datetime = time.struct_time("
        "(t[0], t[1], t[2], t[4], t[5], t[6], t[3], -1, -1))\n"
        " else:\n"
        "  machine.RTC().datetime(t)\n"
        " return t\n"
    )

    def _apply_rtc(self, t: Any, *, force: bool = False) -> Optional[list[int]]:
        """Set the board's RTC to the host's UTC time; return what was set.

        With ``force`` false (every connect), a clock that is already set is
        left alone and None comes back: a connect must not move a clock NTP
        set (mpftp#58). ``force`` is ``mpftp rtc --set``, which always sets.
        """
        tup = self._host_rtc_tuple()
        # One round trip, and nothing left behind in the REPL's globals.
        out = t.exec(
            self._RTC_SET_CODE
            + f"print(_mpftp_rtc_set({tup}, {bool(force)}, {self.RTC_SET_YEAR})"
            " is not None)\n"
            "del _mpftp_rtc_set\n"
        )
        if isinstance(out, (bytes, bytearray)):
            out = out.decode("utf-8", "replace")
        lines = str(out or "").strip().splitlines()
        return list(tup) if lines and lines[-1].strip() == "True" else None

    @staticmethod
    def _serial_flush(serial: Any) -> None:
        try:
            n = serial.inWaiting()
            while n > 0:
                serial.read(n)
                n = serial.inWaiting()
        except Exception:
            pass

    def _reset_esp_to_app(self, serial: Any) -> None:
        """Pulse EN while IO0 is released so the chip boots firmware (not ROM download).

        esptool Detect/flash leave many ESP boards in ``waiting for download``;
        Connect must recover without requiring a physical RESET button.
        """
        import time

        if isinstance(serial, (webrepl.WebSocketSerial, ble.BleSerial)):
            return  # no EN/IO0 lines over the air

        try:
            serial.dtr = False  # IO0 released (not held for download)
            serial.rts = True  # EN low
            time.sleep(0.1)
            serial.rts = False  # EN high → boot app
            time.sleep(0.05)
        except Exception:
            try:
                serial.setDTR(False)
                serial.setRTS(True)
                time.sleep(0.1)
                serial.setRTS(False)
            except Exception:
                pass
        time.sleep(1.6)  # USB-UART boards need time for ROM + MP boot banner
        self._serial_flush(serial)

    def _serial_peek(self, serial: Any, wait: float = 0.15) -> bytes:
        import time

        time.sleep(wait)
        try:
            n = serial.inWaiting()
            return serial.read(n) if n else b""
        except Exception:
            return b""

    @staticmethod
    def _serial_shows_fs_corrupt(data: bytes) -> bool:
        return any(m in data for m in _FS_CORRUPT_MARKERS)

    def _take_control(
        self, t: Any, *, clean: bool = True, timeout_overall: float = 20.0
    ) -> None:
        """Interrupt any running program and enter raw REPL.

        Always sends Ctrl-C (Thonny-style interrupt-on-connect). When ``clean``
        is True:

        - MicroPython: soft-reset *while in raw REPL* so ``main.py`` does not run.
        - CircuitPython: friendly↔raw toggle (no Ctrl-D). CP runs ``code.py`` after
          a raw soft-reboot; switching REPL modes refreshes the VM instead.

        Exception: a corrupt on-board filesystem makes soft-reset re-run
        ``_boot``/``inisetup``, which loops printing the corruption banner and
        never returns to raw REPL. In that case we interrupt + enter raw
        without soft-reset and set ``filesystem_warning`` (MicroPython only).
        """
        import time

        serial = getattr(t, "serial", None)
        if serial is None:
            raise RuntimeError("transport has no serial port")

        saw_fs_corrupt = False

        def note_bytes(data: bytes) -> None:
            nonlocal saw_fs_corrupt
            if data and self._serial_shows_fs_corrupt(data):
                saw_fs_corrupt = True

        def ctrl_c() -> None:
            try:
                serial.write(b"\r\x03")
            except Exception as e:
                if is_dead_serial_error(e):
                    raise RuntimeError(
                        f"serial handle dead during interrupt: {e}"
                    ) from e

        def poke_enter_repl_key(data: bytes) -> None:
            # CircuitPython waits for any key before showing the friendly REPL.
            if data_has_enter_repl_prompt(data):
                try:
                    serial.write(b"\r")
                except Exception as e:
                    if is_dead_serial_error(e):
                        raise RuntimeError(
                            f"serial handle dead during REPL poke: {e}"
                        ) from e
                time.sleep(0.15)
                try:
                    n = serial.inWaiting()
                    if n:
                        note_bytes(serial.read(n))
                except Exception as e:
                    if is_dead_serial_error(e):
                        raise RuntimeError(
                            f"serial handle dead during REPL poke: {e}"
                        ) from e

        def interrupt_storm() -> None:
            # Thorough interrupt (see micropython#7867 / Thonny pipkin).
            # Busy timer/display loops (common on P4 boards) need a longer storm.
            for delay in (0.0, 0.05, 0.05, 0.1, 0.1, 0.15, 0.2):
                if delay:
                    time.sleep(delay)
                ctrl_c()
            time.sleep(0.05)
            try:
                n = serial.inWaiting()
                if n:
                    data = serial.read(n)
                    note_bytes(data)
                    poke_enter_repl_key(data)
            except Exception as e:
                if is_dead_serial_error(e):
                    raise RuntimeError(
                        f"serial handle dead during interrupt storm: {e}"
                    ) from e

        # Probe the handle before investing in the handshake.
        try:
            serial.inWaiting()
        except Exception as e:
            if is_dead_serial_error(e):
                raise RuntimeError(f"serial handle dead: {e}") from e
            raise

        # If a prior esptool session left the chip in ROM download mode, reboot
        # into firmware before trying the raw-REPL handshake.
        peek = self._serial_peek(serial, 0.2)
        note_bytes(peek)
        poke_enter_repl_key(peek)
        if b"waiting for download" in peek or b"DOWNLOAD(" in peek:
            self._reset_esp_to_app(serial)
            after = self._serial_peek(serial, 0.3)
            note_bytes(after)
            poke_enter_repl_key(after)

        interrupt_storm()

        def try_raw(soft_reset: bool, per: float) -> bool:
            try:
                t.enter_raw_repl(soft_reset=soft_reset, timeout_overall=per)
                return True
            except Exception:
                try:
                    n = serial.inWaiting()
                    if n:
                        data = serial.read(n)
                        note_bytes(data)
                        poke_enter_repl_key(data)
                except Exception:
                    pass
                raise

        last_err: Optional[Exception] = None
        # Always enter raw *without* soft-reset first so CircuitPython does not
        # auto-run code.py. Clean behavior is applied after interpreter detect.
        if clean and saw_fs_corrupt:
            try:
                if try_raw(False, 5.0):
                    self.filesystem_warning = (
                        "On-board filesystem is corrupted. Connect succeeded without "
                        "soft-reset; erase flash and reflash MicroPython (Firmware > "
                        "Erase) to restore a usable filesystem."
                    )
                    self._finish_clean_after_raw(t, saw_fs_corrupt=True)
                    return
            except Exception as e:
                last_err = e

        # Two short tries, then one app-mode reset (covers silent download mode
        # where the banner was already drained), then two more tries.
        schedule = [
            ("try", 3.5),
            ("try", 3.5),
            ("reset", 0.0),
            ("try", 5.0),
            ("try", 5.0),
        ]
        for action, per in schedule:
            if action == "reset":
                self._reset_esp_to_app(serial)
                after = self._serial_peek(serial, 0.3)
                note_bytes(after)
                poke_enter_repl_key(after)
                interrupt_storm()
                # After reset the board may start the corrupt-FS boot loop.
                if clean and saw_fs_corrupt:
                    try:
                        if try_raw(False, 5.0):
                            self.filesystem_warning = (
                                "On-board filesystem is corrupted. Connect succeeded "
                                "without soft-reset; erase flash and reflash MicroPython "
                                "(Firmware > Erase) to restore a usable filesystem."
                            )
                            self._finish_clean_after_raw(t, saw_fs_corrupt=True)
                            return
                    except Exception as e:
                        last_err = e
                    continue
            try:
                if try_raw(False, per):
                    self._finish_clean_after_raw(
                        t, saw_fs_corrupt=saw_fs_corrupt, want_clean=clean
                    )
                    return
            except Exception as e:
                last_err = e
                try:
                    t.in_raw_repl = False
                except Exception:
                    pass
                for _ in range(4):
                    ctrl_c()
                    time.sleep(0.08)
                try:
                    n = serial.inWaiting()
                    if n:
                        data = serial.read(n)
                        note_bytes(data)
                        poke_enter_repl_key(data)
                except Exception:
                    pass
                try:
                    serial.write(b"\r\x01")  # poke raw REPL (Thonny)
                except Exception:
                    pass
                time.sleep(0.1)

        # Last resort: interrupt-only enter raw.
        if clean:
            interrupt_storm()
            try:
                if try_raw(False, 5.0):
                    if saw_fs_corrupt:
                        self.filesystem_warning = (
                            "On-board filesystem is corrupted. Connect succeeded without "
                            "soft-reset; erase flash and reflash MicroPython (Firmware > "
                            "Erase) to restore a usable filesystem."
                        )
                    self._finish_clean_after_raw(
                        t, saw_fs_corrupt=saw_fs_corrupt, want_clean=clean
                    )
                    return
            except Exception as e:
                last_err = e

        detail = str(last_err.args[0] if last_err and getattr(last_err, "args", None) else last_err)
        raise RuntimeError(
            self._friendly_take_control_error(detail, fs_corrupt=saw_fs_corrupt)
        ) from last_err

    def _detect_interpreter(self, t: Any) -> str:
        """Read ``sys.implementation.name`` while already in raw REPL."""
        try:
            t.exec("import sys")
            name = t.eval("sys.implementation.name")
            interpreter = normalize_interpreter_name(name)
        except Exception:
            interpreter = self.interpreter or "micropython"
        self.interpreter = interpreter
        return interpreter

    def _finish_clean_after_raw(
        self,
        t: Any,
        *,
        saw_fs_corrupt: bool = False,
        want_clean: bool = True,
    ) -> None:
        """After raw REPL is open: detect interpreter and apply clean semantics."""
        interpreter = self._detect_interpreter(t)
        if not want_clean:
            return
        if interpreter == "circuitpython":
            # Thonny CP clear_repl: exit raw → enter raw (no Ctrl-D soft-reboot).
            try:
                if t.in_raw_repl:
                    t.exit_raw_repl()
            except Exception:
                pass
            try:
                t.in_raw_repl = False
            except Exception:
                pass
            t.enter_raw_repl(soft_reset=False, timeout_overall=10.0)
            try:
                t.exec(
                    "try:\n"
                    " import supervisor\n"
                    " if hasattr(supervisor, 'disable_autoreload'):\n"
                    "  supervisor.disable_autoreload()\n"
                    " elif hasattr(supervisor, 'runtime'):\n"
                    "  supervisor.runtime.autoreload = False\n"
                    "except Exception:\n"
                    " pass\n"
                )
            except Exception:
                pass
            return
        # MicroPython: raw soft-reset skips main.py (unless corrupt FS).
        if saw_fs_corrupt:
            return
        device_name = getattr(t, "device_name", None)
        if webrepl.is_network_device(device_name) or ble.is_ble_device(device_name):
            # A soft reset frees every socket, WebREPL's included, and turns
            # Bluetooth off, so over the air "clean" stops at the interrupt:
            # the VM keeps its state.
            return
        try:
            if t.in_raw_repl:
                t.exit_raw_repl()
        except Exception:
            pass
        try:
            t.in_raw_repl = False
        except Exception:
            pass
        t.enter_raw_repl(soft_reset=True, timeout_overall=10.0)
    @staticmethod
    def _friendly_port_open_error(device: str, exc: BaseException) -> str:
        if isinstance(exc, webrepl.WebReplError):
            return str(exc)  # already says what to check on the network
        raw = str(exc.args[0] if getattr(exc, "args", None) else exc)
        low = raw.lower()
        locked = (
            "exclusively lock" in low
            or "exclusive" in low and "lock" in low
            or "permissionerror" in low
            or "access is denied" in low
            or "permission denied" in low
            or "failed to access" in low
            or "busy" in low
        )
        if locked:
            return (
                f"failed to open {device}: port is busy or locked. "
                f"Another Cursor/mpftp window may already own this COM port "
                f"(one board per session), or Thonny/a serial monitor is holding it. "
                f"Disconnect there, then try again. ({raw})"
            )
        return f"failed to access {device}: {raw}"

    @staticmethod
    def _friendly_take_control_error(detail: str, *, fs_corrupt: bool = False) -> str:
        if fs_corrupt or any(
            s in detail.lower()
            for s in ("filesystem appears to be corrupted", "fs_corrupted")
        ):
            return (
                f"could not take control of the board: on-board filesystem is corrupted "
                f"({detail}). Erase flash and reflash firmware "
                f"(Firmware panel > Erase for MicroPython), then Connect again."
            )
        if is_dead_serial_error(RuntimeError(detail)) or "serial handle dead" in detail.lower():
            return (
                f"could not take control of the board: serial handle is dead "
                f"({detail}). mpftp should have released the COM port — "
                f"Disconnect, then Connect/Resume. If it still says busy, "
                f"reload the extension window or unplug/replug USB."
            )
        return (
            f"could not take control of the board (interrupt/raw REPL failed): {detail}\n"
            f"Device is busy or does not respond. Your options:\n"
            f"  - wait until it completes current work;\n"
            f"  - hard-reset the board and try again;\n"
            f"  - check for a runaway main.py / code.py;\n"
            f"  - confirm firmware is MicroPython or CircuitPython (not bootloader/UF2);\n"
            f"  - close other serial tools holding the port."
        )

    @staticmethod
    def _friendly_probe_error(device: str, exc: BaseException) -> str:
        detail = str(exc.args[0] if getattr(exc, "args", None) else exc)
        if "filesystem is corrupted" in detail.lower():
            return f"{device}: {detail}"
        base = (
            f"{device} is not responding as MicroPython/CircuitPython "
            f"(likely bootloader/UF2 mode, wrong port, or busy REPL): {detail}"
        )
        # Take-control failures already include the checklist.
        if "could not take control" in detail or "Your options:" in detail:
            return base
        return (
            f"{base}\n"
            f"  - wait / hard-reset / check main.py or code.py / confirm firmware / close other tools."
        )

    def _sync_remote_fs(self, t: Any) -> None:
        """Flush on-board filesystem after writes (mpremote fs_writefile does not sync)."""
        try:
            t.exec("import os\nif hasattr(os, 'sync'):\n os.sync()")
        except Exception:
            pass

    def _probe_micropython(self, t: Any) -> Optional[list[int]]:
        """Take control, detect interpreter, set an unset RTC, leave raw REPL.

        Connect never leaves user code running: interrupt is unconditional.
        MicroPython gets a raw soft-reset (skip main.py); CircuitPython gets a
        friendly↔raw toggle (Ctrl-D would run code.py).
        """
        self._take_control(t, clean=True, timeout_overall=20.0)
        rtc: Optional[list[int]] = None
        try:
            rtc = self._apply_rtc(t)
        except Exception:
            # Some ports lack machine.RTC / rtc.RTC; don't fail the connection.
            pass
        self.board_identity = None
        if (self.interpreter or "micropython") == "micropython":
            try:
                self.board_identity = wifiboard.parse_identity(t.exec(wifiboard.IDENTITY_CODE))
            except Exception:
                pass  # identity is for remembering Wi-Fi boards; never fail a connect
        try:
            if t.in_raw_repl:
                t.exit_raw_repl()
        except Exception:
            pass
        return rtc

    def disconnect(self) -> None:
        with self._lock:
            self.debug_tee_stop()
            if self._pm_saved is not None and self.transport is not None:
                # A transfer_begin without its transfer_end: put power save back.
                self._pm_depth = 1
                try:
                    self.with_raw(lambda t: self._pm_exit(t))
                except Exception:
                    pass
            self._pm_depth = 0
            self._pm_saved = None
            self._force_close_transport(graceful=True)

    def _force_close_transport(self, *, graceful: bool = False) -> Optional[str]:
        """Dispose the serial handle. Preserves ``last_device`` for resume.

        When ``graceful`` is True, attempt exit-raw / umount first. Always close
        the underlying pyserial port so Windows COM locks are released.
        """
        self._stop_repl_reader()
        t = self.transport
        device = self.device or self.last_device
        if device:
            self.last_device = device
        self.transport = None
        self.device = None
        self.interpreter = None
        self._repl_mode = False
        self.filesystem_warning = None
        if t is None:
            return device
        if graceful:
            try:
                if getattr(t, "mounted", False):
                    if not t.in_raw_repl:
                        t.enter_raw_repl(soft_reset=False)
                    t.umount_local()
                if t.in_raw_repl:
                    t.exit_raw_repl()
            except Exception:
                pass
        serial = getattr(t, "serial", None)
        if serial is not None:
            try:
                serial.close()
            except Exception:
                pass
        try:
            t.close()
        except Exception:
            pass
        return device

    def _release_dead_transport(self, reason: str) -> Optional[str]:
        """Close a wedged handle and notify clients (mpftp#3 recovery)."""
        device = self.device or self.last_device
        _notify(
            "transport_dead",
            {"device": device, "message": reason, "pid": os.getpid()},
        )
        return self._force_close_transport(graceful=False)

    def _reclaim_session(self, *, clean: bool = True) -> Any:
        """Reopen ``last_device`` and take control after a dead-handle release."""
        import time

        device = self.last_device
        if not device:
            raise RuntimeError(
                "serial handle released but no last device to reclaim; Connect again"
            )
        last_err: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                t = self._open_transport(device, self.baud or 115200)
                _bound_write_timeout(t)
                self.transport = t
                self.device = device
                self.last_device = device
                self._take_control(t, clean=clean, timeout_overall=20.0)
                return t
            except Exception as e:
                last_err = e
                self._force_close_transport(graceful=False)
                time.sleep(0.35 * attempt)
        detail = str(
            last_err.args[0] if last_err and getattr(last_err, "args", None) else last_err
        )
        raise RuntimeError(
            f"failed to reclaim {device} after serial handle death: {detail}"
        ) from last_err

    def _take_control_resilient(
        self, t: Any, *, clean: bool = True, timeout_overall: float = 20.0
    ) -> Any:
        """``_take_control`` with one recycle+retry when the COM handle is dead."""
        try:
            self._take_control(t, clean=clean, timeout_overall=timeout_overall)
            return self._require()
        except Exception as e:
            if not (is_dead_serial_error(e) or "serial handle dead" in str(e).lower()):
                if self.is_remote:
                    # The link is fine but the REPL won't come back: a loop
                    # that never yields keeps WebREPL from reading Ctrl-C.
                    raise RuntimeError(
                        f"{self.device or self.last_device}: {wifiboard.STUCK_MESSAGE}"
                    ) from e
                raise
            self._release_dead_transport(str(e))
            return self._reclaim_session(clean=clean)

    def _require(self):
        if not self.transport:
            raise RuntimeError("not connected")
        return self.transport

    def _enter_raw(self, soft_reset: bool = False) -> None:
        t = self._require()
        if self._repl_mode:
            self._stop_repl_reader()
        if not t.in_raw_repl:
            # Prefer full take-control (interrupt storm) over a bare enter_raw —
            # busy UI loops otherwise fail with "could not enter raw repl".
            if soft_reset:
                self._take_control_resilient(t, clean=True, timeout_overall=20.0)
            else:
                try:
                    t.enter_raw_repl(soft_reset=False, timeout_overall=8.0)
                except Exception:
                    self._take_control_resilient(t, clean=False, timeout_overall=20.0)

    def _leave_raw_to_repl(self) -> None:
        t = self._require()
        if t.in_raw_repl:
            t.exit_raw_repl()
        if self._repl_mode:
            self._start_repl_reader()

    def _restore_repl_if_wanted(self) -> None:
        if not self._repl_mode or not self.transport:
            return
        try:
            if self.transport.in_raw_repl:
                self.transport.exit_raw_repl()
        except Exception:
            pass
        self._start_repl_reader()

    def with_raw(self, fn, soft_reset: bool = False):
        """Pause friendly REPL, run ``fn`` in raw REPL, then restore REPL."""
        with self._lock:
            was_repl = self._repl_mode
            try:
                self._enter_raw(soft_reset=soft_reset)
                return fn(self._require())
            except Exception as e:
                if is_eof_timeout_error(e):
                    self._release_dead_transport(str(e))
                    raise RuntimeError(friendly_exec_timeout_message(str(e))) from e
                if is_dead_serial_error(e) or "serial handle dead" in str(e).lower():
                    self._release_dead_transport(str(e))
                    # One reclaim + retry for filesystem / short ops.
                    self._reclaim_session(clean=False)
                    self._enter_raw(soft_reset=soft_reset)
                    return fn(self._require())
                raise
            finally:
                if self.transport and (was_repl or self._repl_mode):
                    self._repl_mode = True
                    self._restore_repl_if_wanted()
                elif not self.transport:
                    self._repl_mode = False

    # --- Wi-Fi power save during transfers ---

    def _pm_enter(self, t: Any) -> None:
        """Turn Wi-Fi power save off for a transfer, remembering the value read.

        Only over WebREPL, only on MicroPython, and never while Bluetooth is
        active (ESP-IDF needs modem sleep for Wi-Fi/BLE coexistence). Nested
        calls (a transfer_begin around several fs_write calls) share one change.
        """
        self._pm_depth += 1
        if self._pm_depth > 1:
            return
        self._pm_saved = None
        self._pm_report = None
        if not self.is_network or (self.interpreter or "micropython") != "micropython":
            return
        try:
            got = wifiboard.parse_pm(t.exec(wifiboard.PM_READ_CODE))
        except Exception:
            got = None
        if got is None:
            self._pm_report = {"changed": False, "reason": "could not read the setting"}
            return
        pm, pm_none, ble = got
        if self._pm_orphan is not None:
            pm, self._pm_orphan = self._pm_orphan, None
        if ble:
            self._pm_report = {"changed": False, "was": pm, "reason": "Bluetooth is active"}
            return
        if pm == pm_none:
            self._pm_report = {"changed": False, "was": pm, "reason": "already off"}
            return
        t.exec(wifiboard.pm_set_code(pm_none))
        self._pm_saved = pm
        self._pm_report = {"changed": True, "was": pm, "during": pm_none}

    def _pm_exit(self, t: Any) -> Optional[dict[str, Any]]:
        """Put back exactly the value ``_pm_enter`` read. Returns the report."""
        self._pm_depth = max(0, self._pm_depth - 1)
        if self._pm_depth > 0:
            return None
        report = self._pm_report
        saved, self._pm_saved = self._pm_saved, None
        if saved is None:
            return report
        report = dict(report or {})
        try:
            t.exec(wifiboard.pm_set_code(saved))
            report["restored"] = saved
        except Exception as e:
            self._pm_orphan = saved
            report["restored"] = None
            report["note"] = (
                f"could not put Wi-Fi power save back to {saved} ({e}); it stays off "
                "until the board resets"
            )
        return report

    def _transfer(self, fn):
        """Wrap a with_raw op so Wi-Fi power save is off while it runs.

        The restore sits in a ``finally``: an error or a cancel puts the value
        back too. If the connection itself dropped, the board keeps power save
        off until its next reset, and the error says so.
        """

        def op(t):
            self._pm_enter(t)
            try:
                result = fn(t)
            except Exception as e:
                try:
                    report = self._pm_exit(self.transport or t)
                except Exception:
                    report = None
                if report and report.get("note"):
                    raise RuntimeError(f"{e} ({report['note']})") from e
                raise
            report = self._pm_exit(t)
            if report is not None and isinstance(result, dict):
                result = {**result, "power_save": report}
            return result

        return op

    def transfer_begin(self) -> dict[str, Any]:
        """Hold power save off across several transfer calls (a UI's batch)."""
        return self.with_raw(lambda t: (self._pm_enter(t), {"power_save": self._pm_report})[1])

    def transfer_end(self) -> dict[str, Any]:
        if self._pm_depth == 0:
            return {"power_save": None}
        return self.with_raw(lambda t: {"power_save": self._pm_exit(t)})

    # --- Wi-Fi access: identity, boot.py, discovery ---

    def wifi_status(self) -> dict[str, Any]:
        """The board's identity and Wi-Fi address now (uid, hostname, ip, webrepl)."""
        self._require_micropython("wifi_status")

        def op(t):
            identity = wifiboard.parse_identity(t.exec(wifiboard.IDENTITY_CODE)) or {}
            self.board_identity = identity or self.board_identity
            return {"board": identity}

        return self.with_raw(op)

    def _read_boot(self, t: Any) -> Optional[str]:
        try:
            if not t.fs_exists(wifiboard.BOOT_PATH):
                return None
        except Exception:
            pass
        try:
            return bytes(t.fs_readfile(wifiboard.BOOT_PATH)).decode("utf-8")
        except UnicodeDecodeError as e:
            raise RuntimeError("boot.py isn't UTF-8 text; mpftp won't edit it") from e
        except Exception as e:
            if "ENOENT" in str(e) or "No such file" in str(e):
                return None
            raise

    def _wifi_access_change(
        self, t: Any, action: str, password: Optional[str]
    ) -> dict[str, Any]:
        """Work out what enable/disable would do to boot.py, without writing."""
        if action not in ("enable", "disable"):
            raise RuntimeError(f"wifi access: unknown action {action!r}")
        if action == "enable" and self.is_network:
            raise RuntimeError(
                "Enable Wi-Fi access over a serial (USB) connection: it writes boot.py, "
                "and the board needs to be reachable if Wi-Fi doesn't come up."
            )
        current = self._read_boot(t)
        problems: list[str] = []
        new: Optional[str] = current
        if action == "enable":
            try:
                wifiboard.check_new_password(password)
            except ValueError as e:
                raise RuntimeError(str(e)) from e
            prereqs = wifiboard.parse_identity(t.exec(wifiboard.PREREQ_CODE)) or {}
            problems = wifiboard.prereq_problems(prereqs)
            if current is not None and wifiboard.find_block(current):
                problems.append(
                    "boot.py already has mpftp's Wi-Fi block. Disable Wi-Fi access first "
                    "to change the password."
                )
            if not problems:
                new = wifiboard.add_block(current, password or "")
        else:
            if current is None or not wifiboard.find_block(current):
                problems.append("boot.py has no mpftp Wi-Fi block, so there is nothing to remove.")
            else:
                new = wifiboard.remove_block(current)

        def shown(text: Optional[str]) -> Optional[str]:
            if text is None:
                return None
            return wifiboard.mask_block_passwords(wifiboard.mask_password(text, password))

        return {
            "action": action,
            "path": wifiboard.BOOT_PATH,
            "exists": current is not None,
            "delete": current is not None and new is None,
            "problems": problems,
            "sha256": wifiboard.sha256_text(current),
            "current_masked": shown(current),
            "proposed_masked": shown(new) if not problems else None,
            "diff": wifiboard.unified_diff(shown(current), shown(new)) if not problems else "",
            "_new": new,
        }

    def wifi_access_plan(self, action: str, password: Optional[str] = None) -> dict[str, Any]:
        """The boot.py change "Enable/Disable Wi-Fi access" would make, for the
        user to read before saying yes. Passwords are masked in what it returns."""
        self._require_micropython("Wi-Fi access")

        def op(t):
            plan = self._wifi_access_change(t, action, password)
            plan.pop("_new")
            return plan

        return self.with_raw(op)

    def wifi_access_apply(
        self,
        action: str,
        password: Optional[str] = None,
        expect_sha256: Optional[str] = None,
        now: bool = False,
    ) -> dict[str, Any]:
        """Write the change the user confirmed. ``expect_sha256`` is the plan's
        ``sha256``: if boot.py changed since, nothing is written. ``now`` also
        starts (enable) or stops (disable) WebREPL without a reset."""
        self._require_micropython("Wi-Fi access")

        def op(t):
            plan = self._wifi_access_change(t, action, password)
            if plan["problems"]:
                raise RuntimeError(" ".join(plan["problems"]))
            if plan["sha256"] != (expect_sha256 or None):
                raise RuntimeError(
                    "boot.py changed since you reviewed the change; nothing was written. "
                    "Run it again to see the current version."
                )
            new = plan["_new"]
            if new is None:
                t.fs_rmfile(wifiboard.BOOT_PATH)
            else:
                t.fs_writefile(wifiboard.BOOT_PATH, new.encode("utf-8"))
            self._sync_remote_fs(t)
            after = self._read_boot(t)
            if after != new:
                raise RuntimeError("boot.py doesn't read back as written; check the board's filesystem")
            result: dict[str, Any] = {
                "ok": True,
                "action": action,
                "path": wifiboard.BOOT_PATH,
                "deleted": new is None,
                "sha256": wifiboard.sha256_text(after),
            }
            if now and action == "enable":
                code = wifiboard.START_NOW_CODE.format(end=wifiboard.BLOCK_END)
                out, err = t.exec_raw(code, timeout=60)
                # Keep only the address line: the helper's messages name the network.
                result["ip"] = wifiboard.parse_start_now(out)
                if err:
                    result["started"] = False
                else:
                    result["started"] = True
                identity = wifiboard.parse_identity(t.exec(wifiboard.IDENTITY_CODE))
                if identity:
                    self.board_identity = identity
                    result["board"] = identity
            elif now and action == "disable":
                t.exec("import webrepl\nwebrepl.stop()")
                result["stopped"] = True
            return result

        return self.with_raw(op)

    def mdns_resolve(self, name: str, timeout: float = 1.5) -> dict[str, Any]:
        """``<hostname>.local`` to an address, from wherever the sidecar runs
        (on WSL that is Windows Python, whose resolver speaks mDNS)."""
        try:
            return mdns.resolve(name, timeout=float(timeout))
        except ValueError as e:
            raise RuntimeError(str(e)) from e

    # --- filesystem ---

    def _require_micropython(self, feature: str) -> None:
        interpreter = self.interpreter or "micropython"
        if interpreter != "micropython":
            raise RuntimeError(
                f"{feature} is MicroPython-only (connected interpreter is {interpreter})"
            )

    def _board_listdir(self, t: Any, path: str) -> list[Any]:
        """List directory entries on the board (listdir, then ilistdir).

        Returns mpremote-like objects with ``.name``, ``.st_mode``, ``.st_size``.
        Prefers ``os.listdir`` + ``os.stat`` (universal), falls back to
        ``os.ilistdir`` when listdir is missing — Thonny's order.
        """
        list_path = "" if path in ("", "/", None) else path
        # Prefer transport helper when present and working (MicroPython).
        try:
            return list(t.fs_listdir(list_path))
        except Exception:
            pass

        path_lit = repr(list_path if list_path else "/")
        code = f"""
import os
_p = {path_lit}
_out = []
_ok = False
if hasattr(os, 'listdir'):
 try:
  for _n in os.listdir(_p):
   if _n in ('.', '..'):
    continue
   _fp = (_p.rstrip('/') + '/' + _n) if _p not in ('', '/') else ('/' + _n)
   try:
    _st = os.stat(_fp)
    _out.append((_n, _st[0], 0, _st[6] if len(_st) > 6 else 0))
   except Exception:
    _out.append((_n, 0x8000, 0, 0))
  _ok = True
 except Exception:
  _out = []
if not _ok:
 for _e in os.ilistdir(_p):
  _n = _e[0]
  if _n in ('.', '..'):
   continue
  _mode = _e[1] if len(_e) > 1 else 0x8000
  _size = _e[3] if len(_e) > 3 else 0
  _out.append((_n, _mode, 0, _size))
print(repr(_out))
"""
        out = t.exec(code)
        text = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
        line = text.strip().splitlines()[-1] if text.strip() else "[]"
        import ast

        rows = ast.literal_eval(line)

        class _Ent:
            __slots__ = ("name", "st_mode", "st_ino", "st_size")

            def __init__(self, name: str, mode: int, ino: int, size: int) -> None:
                self.name = name
                self.st_mode = mode
                self.st_ino = ino
                self.st_size = size

        return [_Ent(*r) if len(r) >= 4 else _Ent(r[0], r[1] if len(r) > 1 else 0x8000, 0, 0) for r in rows]

    def fs_listdir(self, path: str = "/") -> list[dict[str, Any]]:
        # mpremote uses "" for the device root; "/" also works on most ports.
        list_path = "" if path in ("", "/", None) else path

        def op(t):
            entries = []
            for e in self._board_listdir(t, list_path):
                is_dir = bool(e.st_mode & 0x4000)
                entries.append(
                    {
                        "name": e.name,
                        "isDir": is_dir,
                        "size": e.st_size,
                        "mode": e.st_mode,
                    }
                )
            entries.sort(key=lambda x: (not x["isDir"], x["name"].lower()))
            return entries

        return self.with_raw(op)

    def fs_stat(self, path: str) -> dict[str, Any]:
        def op(t):
            st = t.fs_stat(path)
            return {
                "mode": st.st_mode,
                "size": st.st_size,
                "isDir": bool(st.st_mode & 0x4000),
            }

        return self.with_raw(op)

    def fs_read(self, path: str) -> dict[str, Any]:
        def op(t):
            data = t.fs_readfile(path)
            return {
                "path": path,
                "size": len(data),
                "data_b64": base64.b64encode(bytes(data)).decode("ascii"),
            }

        return self.with_raw(self._transfer(op))

    def _board_mpy_version(self, t: Any) -> int:
        """``sys.implementation._mpy & 0xFF`` — 0 if the board doesn't expose it."""
        try:
            t.exec("import sys")
            raw = t.eval("getattr(sys.implementation, '_mpy', 0) & 0xFF")
            return int(raw)
        except Exception:
            return 0

    def _prepare_mpy_compile(self, t: Any) -> str:
        """Resolve mpy-cross and check its bytecode version against the board."""
        self._require_micropython("compile-on-upload")
        exe = find_mpy_cross(
            micropython_hint=config.resolve("micropythonPath") or None,
            workspace=config.resolve("workspacePath") or None,
        )
        cross_ver = mpy_cross_version(exe)
        board_ver = self._board_mpy_version(t)
        if board_ver and board_ver != cross_ver[0]:
            raise RuntimeError(
                f"mpy-cross ({exe}) emits mpy v{cross_ver[0]}.{cross_ver[1]}, but the "
                f"connected board expects mpy v{board_ver}. Use an mpy-cross built from "
                "the same MicroPython tree as the board's firmware."
            )
        return exe

    def _compile_to_mpy(self, exe: str, name: str, data: bytes) -> tuple[str, bytes]:
        """Run mpy-cross on ``.py`` bytes; return (new basename, compiled bytes)."""
        with tempfile.TemporaryDirectory(prefix="mpftp-mpy-") as td:
            src = Path(td) / name
            src.write_bytes(data)
            out_path = src.with_suffix(".mpy")
            proc = subprocess.run(
                [exe, "-o", str(out_path), str(src)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()
                raise RuntimeError(f"mpy-cross failed on {name}: {detail}")
            return out_path.name, out_path.read_bytes()

    def fs_write(
        self, path: str, data_b64: str, mpy: bool = False, verify: bool = False
    ) -> dict[str, Any]:
        data = base64.b64decode(data_b64)
        dest_path = path
        host = self._circuitpy_host_path(path)
        if host is not None:
            if mpy:
                raise RuntimeError(
                    "compile-on-upload is MicroPython-only (connected interpreter is circuitpython)"
                )
            host.parent.mkdir(parents=True, exist_ok=True)
            host.write_bytes(data)
            return {"path": path, "size": len(data), "via": "circuitpy_msc"}

        def op(t):
            nonlocal data, dest_path
            self._ensure_cp_writable(t)
            compiled = False
            if mpy:
                name = Path(dest_path).name
                exclude = set(config.resolve("mpyExcludeFiles"))
                if name.endswith(".py") and name not in exclude:
                    exe = self._prepare_mpy_compile(t)
                    new_name, data = self._compile_to_mpy(exe, name, data)
                    dest_path = _replace_remote_basename(dest_path, new_name)
                    compiled = True
            t.fs_writefile(dest_path, data)
            self._sync_remote_fs(t)
            result: dict[str, Any] = {"path": dest_path, "size": len(data), "compiled": compiled}
            if verify:
                digest = t.fs_hashfile(dest_path, "sha256")
                hx = digest.hex() if isinstance(digest, bytes) else str(digest)
                if hx != host_sha256(data):
                    raise RuntimeError(f"hash mismatch after upload: {dest_path}")
                result["verified"] = hx
            return result

        return self.with_raw(self._transfer(op))

    def fs_mkdir(self, path: str) -> dict[str, Any]:
        host = self._circuitpy_host_path(path)
        if host is not None:
            host.mkdir(parents=True, exist_ok=True)
            return {"path": path, "via": "circuitpy_msc"}

        def op(t):
            t.fs_mkdir(path)
            return {"path": path}

        return self.with_raw(op)

    def fs_rm(self, path: str) -> dict[str, Any]:
        host = self._circuitpy_host_path(path)
        if host is not None:
            if host.is_dir():
                raise RuntimeError(f"is a directory: {path} (use fs_rmdir or fs_rm_rf)")
            if host.exists():
                host.unlink()
            return {"path": path, "via": "circuitpy_msc"}

        def op(t):
            if t.fs_isdir(path):
                raise RuntimeError(f"is a directory: {path} (use fs_rmdir or fs_rm_rf)")
            t.fs_rmfile(path)
            return {"path": path}

        return self.with_raw(op)

    def fs_rmdir(self, path: str) -> dict[str, Any]:
        host = self._circuitpy_host_path(path)
        if host is not None:
            host.rmdir()
            return {"path": path, "via": "circuitpy_msc"}

        def op(t):
            t.fs_rmdir(path)
            return {"path": path}

        return self.with_raw(op)

    def fs_rm_rf(self, path: str) -> dict[str, Any]:
        host = self._circuitpy_host_path(path)
        if host is not None:
            if path in ("", "/", "."):
                raise RuntimeError("refusing to rm -rf CIRCUITPY root via MSC")
            if host.is_dir():
                shutil.rmtree(host)
            elif host.exists():
                host.unlink()
            return {"path": path, "via": "circuitpy_msc"}

        def _rm(t, p: str) -> None:
            if t.fs_isdir(p):
                for e in self._board_listdir(t, p):
                    child = p.rstrip("/") + "/" + e.name if p not in ("", ".") else e.name
                    if p == "/":
                        child = "/" + e.name
                    _rm(t, child)
                if p not in ("", "/", "."):
                    t.fs_rmdir(p)
            else:
                t.fs_rmfile(p)

        def op(t):
            _rm(t, path)
            return {"path": path}

        return self.with_raw(op)

    def fs_touch(self, path: str) -> dict[str, Any]:
        host = self._circuitpy_host_path(path)
        if host is not None:
            host.parent.mkdir(parents=True, exist_ok=True)
            host.touch(exist_ok=True)
            return {"path": path, "via": "circuitpy_msc"}

        def op(t):
            t.fs_touchfile(path)
            return {"path": path}

        return self.with_raw(op)

    def fs_rename(self, src: str, dest: str) -> dict[str, Any]:
        host_src = self._circuitpy_host_path(src)
        host_dest = self._circuitpy_host_path(dest)
        if host_src is not None and host_dest is not None:
            host_dest.parent.mkdir(parents=True, exist_ok=True)
            host_src.rename(host_dest)
            return {"src": src, "dest": dest, "via": "circuitpy_msc"}

        def op(t):
            # mpremote Transport has no fs_rename; use device os.rename.
            t.exec(f"import os\nos.rename({src!r}, {dest!r})")
            return {"src": src, "dest": dest}

        return self.with_raw(op)

    def fs_hash(self, path: str, algo: str = "sha256") -> dict[str, Any]:
        def op(t):
            digest = t.fs_hashfile(path, algo)
            digest_hex = digest.hex() if isinstance(digest, bytes) else str(digest)
            return {"path": path, "algo": algo, "hash": digest_hex}

        return self.with_raw(op)

    def fs_tree(self, path: str = "/") -> dict[str, Any]:
        def walk(t, p: str, depth: int = 0) -> list[dict[str, Any]]:
            nodes = []
            try:
                entries = self._board_listdir(t, p if p != "/" else "")
            except Exception:
                return nodes
            for e in sorted(entries, key=lambda x: (not bool(x.st_mode & 0x4000), x.name.lower())):
                is_dir = bool(e.st_mode & 0x4000)
                child_path = (p.rstrip("/") + "/" + e.name) if p != "/" else "/" + e.name
                node = {
                    "name": e.name,
                    "path": child_path,
                    "isDir": is_dir,
                    "size": e.st_size,
                }
                if is_dir and depth < 8:
                    node["children"] = walk(t, child_path, depth + 1)
                nodes.append(node)
            return nodes

        def op(t):
            return {"path": path, "children": walk(t, path)}

        return self.with_raw(op)

    # --- exec / control ---

    def exec(self, code: str, follow: bool = True) -> dict[str, Any]:
        def op(t):
            out = t.exec(code) if follow else t.exec_raw_no_follow(code.encode())
            if out is None:
                text = ""
            elif isinstance(out, bytes):
                text = out.decode("utf-8", "replace")
            else:
                text = str(out)
            return {"output": text, "followed": bool(follow)}

        return self.with_raw(op, soft_reset=False)

    def eval(self, expr: str) -> dict[str, Any]:
        def op(t):
            val = t.eval(expr)
            return {"value": repr(val)}

        return self.with_raw(op)

    def _run_after_clean(
        self, source: str, *, follow: bool, path: Optional[str] = None
    ) -> dict[str, Any]:
        """Interrupt + raw soft-reset (skip main), then exec source."""
        with self._lock:
            was_repl = self._repl_mode
            t = self._require()
            self._stop_repl_reader()
            try:
                t = self._take_control_resilient(t, clean=True, timeout_overall=20.0)
                if follow:
                    result: dict[str, Any] = {"output": exec_follow(t, source), "followed": True}
                else:
                    t.exec_raw_no_follow(source.encode())
                    try:
                        if t.in_raw_repl:
                            t.exit_raw_repl()
                    except Exception:
                        pass
                    result = {"output": "", "followed": False}
                if path is not None:
                    result["path"] = path
                return result
            except Exception as e:
                if is_eof_timeout_error(e) or is_dead_serial_error(e):
                    self._release_dead_transport(str(e))
                    message = friendly_exec_timeout_message(str(e))
                    if isinstance(e, FollowTimeoutError):
                        raise FollowTimeoutError(message, e.partial_output) from e
                    raise RuntimeError(message) from e
                raise
            finally:
                if self.transport and (was_repl or self._repl_mode):
                    self._repl_mode = True
                    self._restore_repl_if_wanted()
                elif not self.transport:
                    self._repl_mode = False

    def run_script(self, source: str, follow: bool = False) -> dict[str, Any]:
        """Run source after interrupt + raw soft-reset (skip main.py).

        Default ``follow=False`` so UI apps do not block the sidecar (same as
        ``run_path``). Pass ``follow=True`` only for short scripts that exit.
        """
        return self._run_after_clean(source, follow=follow)

    def run_path(self, path: str, follow: bool = False) -> dict[str, Any]:
        """
        Run a .py file already on the board.

        Takes control (interrupt + raw soft-reset, skipping main.py), then execs
        the file with __name__/__file__ set like a normal script launch. This is
        how user code is started — Soft Reset alone never runs main.py.
        Default follow=False so UI apps / loops do not block the sidecar; output
        is left on the UART (open REPL to watch).
        """
        path_lit = json.dumps(path)
        code = (
            f"_p = {path_lit}\n"
            "exec(compile(open(_p).read(), _p, \"exec\"), "
            "{\"__name__\": \"__main__\", \"__file__\": _p})\n"
        )
        return self._run_after_clean(code, follow=follow, path=path)

    #: How long a board gets to say anything after Ctrl-C over WebREPL.
    NETWORK_INTERRUPT_WAIT = 2.5
    #: When the board hasn't answered Ctrl-C after this long, it is asked for
    #: something every live REPL answers, and asked again at this interval.
    NETWORK_POKE_AFTER = 0.75
    NETWORK_POKE_EVERY = 0.5

    def _await_network_reply(
        self, serial: Any, before: int, *, raw: bool, pump: Callable[[], bool]
    ) -> Optional[tuple[float, bool]]:
        """Wait for the board to say anything after Ctrl-C over WebREPL.

        Silence isn't proof of a busy board. A program that catches
        KeyboardInterrupt and ends quietly prints nothing, and raw REPL treats
        Ctrl-C as "clear the line" and prints nothing either. So after a short
        wait the board is poked with a key every live REPL answers: Ctrl-B
        (the friendly banner; from raw REPL it also returns to friendly, which
        is where the sidecar thinks the board is), or, when the sidecar holds
        raw REPL, Ctrl-A (the raw banner, staying raw). Only a board that
        answers none of this in ``NETWORK_INTERRUPT_WAIT`` is a loop that
        never yields: WebREPL input is read from the VM's poll hook, which
        such a loop never reaches on firmware without the WebREPL Ctrl-C fix.

        ``pump`` reads the socket (or returns at once when another thread
        does), and returns False when the connection is gone. Gives
        ``(seconds to the first reply, poked)``, or None for silence.
        """
        import time

        start = time.monotonic()
        deadline = start + self.NETWORK_INTERRUPT_WAIT
        next_poke = start + self.NETWORK_POKE_AFTER
        poked = False
        while True:
            if serial.rx_total != before:
                return time.monotonic() - start, poked
            now = time.monotonic()
            if now >= deadline or not pump():
                return None
            if now >= next_poke:
                try:
                    serial.write(b"\x01" if raw else b"\x02")
                except Exception:
                    return None
                poked = True
                next_poke = now + self.NETWORK_POKE_EVERY
            time.sleep(0.02)

    def interrupt(self) -> dict[str, Any]:
        """Send Ctrl-C without resetting or entering raw REPL."""
        with self._lock:
            try:
                t = self._require()
                serial = getattr(t, "serial", None)
                if serial is None:
                    raise RuntimeError("transport has no serial port")
                if isinstance(serial, (webrepl.WebSocketSerial, ble.BleSerial)):
                    return self._network_interrupt(serial)
                serial.write(b"\r\x03")
                return {"ok": True}
            except Exception as e:
                if wifiboard.STUCK_MESSAGE in str(e):
                    raise
                if not (is_dead_serial_error(e) or "serial handle dead" in str(e).lower()):
                    raise RuntimeError(f"interrupt failed: {e}") from e
                self._release_dead_transport(str(e))
                t = self._reclaim_session(clean=False)
                serial = getattr(t, "serial", None)
                if serial is None:
                    raise RuntimeError(
                        "interrupt failed: reclaimed transport has no serial"
                    ) from e
                try:
                    serial.write(b"\r\x03")
                except Exception as e2:
                    raise RuntimeError(f"interrupt failed after reclaim: {e2}") from e2
                return {"ok": True, "reclaimed": True}

    def _network_interrupt(self, serial: Any) -> dict[str, Any]:
        """Ctrl-C over WebREPL, and a check that the board heard it.

        Reports ``latency_ms``, from Ctrl-C to the board's first reply, and
        ``poked`` when the reply only came once the board was asked for one
        (see ``_await_network_reply``). Silence gets the busy-loop message.
        """

        def pump() -> bool:
            try:
                serial.inWaiting()  # pumps the socket; the REPL reader may take the bytes
            except Exception:
                return False
            return True

        t = self.transport
        raw = bool(getattr(t, "in_raw_repl", False)) and not self._repl_mode
        pump()  # count what already arrived as before Ctrl-C, not as its answer
        before = serial.rx_total
        serial.write(b"\r\x03")
        reply = self._await_network_reply(serial, before, raw=raw, pump=pump)
        if reply is None:
            raise RuntimeError(f"{serial.port}: {wifiboard.STUCK_MESSAGE}")
        latency, poked = reply
        result: dict[str, Any] = {"ok": True, "latency_ms": round(latency * 1000)}
        if poked:
            result["poked"] = True
        return result

    def soft_reset(self) -> dict[str, Any]:
        """Fresh session without running user startup scripts the MP way.

        MicroPython: raw soft-reset (does not run main.py).
        CircuitPython: friendly↔raw toggle (Ctrl-D would run code.py).
        """
        if self.is_ble:
            raise RuntimeError(
                "soft-reset over BLE would end the session for good: a soft reset turns "
                "Bluetooth off, and a raw soft reset skips the main.py that starts "
                "bledev.repl. Use soft-reboot (main.py runs, and brings BLE back if it "
                "starts bledev.repl), or hard-reset."
            )
        if self.is_network:
            return self._network_soft_reset(run_main=False)
        with self._lock:
            t = self._require()
            self._stop_repl_reader()
            t = self._take_control_resilient(t, clean=True, timeout_overall=20.0)
            interpreter = self.interpreter or "micropython"
            self._restore_repl_if_wanted()
            return {
                "ok": True,
                "interpreter": interpreter,
                "main_skipped": interpreter == "micropython",
                "runs_main": False,
                "note": (
                    "MicroPython soft-reset skips main.py; use soft-reboot or hard-reset "
                    "to run main.py"
                    if interpreter == "micropython"
                    else "CircuitPython soft-reset does not Ctrl-D (code.py not re-run)"
                ),
            }

    def soft_reboot(self) -> dict[str, Any]:
        """Friendly Ctrl-D soft-reboot so startup scripts run (main.py / code.py).

        Leaves the session in friendly REPL (or reconnect if the port drops).
        Opposite of soft_reset, which skips main.py on MicroPython.
        """
        import time

        if self.is_remote:
            return self._network_soft_reset(run_main=True)
        with self._lock:
            t = self._require()
            self._stop_repl_reader()
            serial = getattr(t, "serial", None)
            if serial is None:
                raise RuntimeError("transport has no serial port")
            try:
                if t.in_raw_repl:
                    t.exit_raw_repl()
            except Exception as e:
                if is_dead_serial_error(e):
                    self._release_dead_transport(str(e))
                    t = self._reclaim_session(clean=False)
                    serial = getattr(t, "serial", None)
                    if serial is None:
                        raise RuntimeError(
                            "soft-reboot failed: no serial after reclaim"
                        ) from e
                    try:
                        if t.in_raw_repl:
                            t.exit_raw_repl()
                    except Exception:
                        pass
            try:
                t.in_raw_repl = False
            except Exception:
                pass
            try:
                serial.write(b"\x04")
            except Exception as e:
                if is_dead_serial_error(e):
                    self._release_dead_transport(str(e))
                    raise RuntimeError(
                        f"soft-reboot failed (serial handle dead): {e}"
                    ) from e
                raise RuntimeError(f"soft-reboot failed: {e}") from e
            time.sleep(1.2)
            try:
                self._serial_flush(serial)
            except Exception:
                pass
            interpreter = self.interpreter or "micropython"
            if self._repl_mode:
                self._start_repl_reader()
            return {
                "ok": True,
                "interpreter": interpreter,
                "main_skipped": False,
                "runs_main": True,
                "note": (
                    "Ctrl-D soft-reboot; MicroPython runs main.py, "
                    "CircuitPython runs code.py"
                ),
            }

    #: How long a soft reset may take to bring WebREPL back (boot.py included).
    NETWORK_RESET_WAIT = 25.0

    def _network_soft_reset(self, *, run_main: bool) -> dict[str, Any]:
        """Soft reset over WebREPL, which ends the session: the reset frees
        every socket. ``run_main`` False (soft_reset) resets from raw REPL so
        main.py is skipped, then reconnects; True (soft_reboot) resets from the
        friendly REPL so main.py runs, and leaves reconnecting to ``resume``,
        since connecting interrupts whatever main.py started.
        """
        import time

        with self._lock:
            t = self._require()
            device = self.device
            self._stop_repl_reader()
            try:
                self._take_control_resilient(t, clean=False, timeout_overall=15.0)
                t = self._require()
                if run_main and t.in_raw_repl:
                    t.exit_raw_repl()
                t.serial.write(b"\x04")
                time.sleep(0.3)
            finally:
                self._force_close_transport(graceful=False)
            if run_main:
                return {
                    "ok": True,
                    "interpreter": "micropython",
                    "main_skipped": False,
                    "runs_main": True,
                    "reconnected": False,
                    "note": (
                        "soft-reboot over the air ends the session; main.py is running. "
                        "`resume` reconnects (and interrupts main.py)"
                    ),
                }
            deadline = time.monotonic() + self.NETWORK_RESET_WAIT
            last_err: Optional[Exception] = None
            time.sleep(1.0)
            while time.monotonic() < deadline:
                try:
                    res = self.connect(device, self.baud, attempts=1)
                    res.update(
                        {
                            "ok": True,
                            "main_skipped": True,
                            "runs_main": False,
                            "reconnected": True,
                            "note": "raw soft-reset skipped main.py; WebREPL reconnected",
                        }
                    )
                    return res
                except Exception as e:
                    last_err = e
                    if "rejected the WebREPL password" in str(e):
                        break
                    time.sleep(1.0)
            raise RuntimeError(
                f"{device}: the board soft-reset but WebREPL did not come back within "
                f"{self.NETWORK_RESET_WAIT:.0f} s ({last_err}). WebREPL only survives a "
                "reset if boot.py connects Wi-Fi and calls webrepl.start()."
            )

    # CircuitPython has no machine module: the MicroPython form raised
    # ImportError inside exec_raw_no_follow, whose result is never read, so a
    # CircuitPython board was told it had reset and kept running.
    _HARD_RESET_CODE = {
        "micropython": "import time, machine; time.sleep_ms(100); machine.reset()",
        "circuitpython": "import time, microcontroller; time.sleep(0.1); microcontroller.reset()",
    }

    def _console_dtr_for(self, device: Optional[str], interpreter: str) -> bool:
        """Look the port up now: a native-USB one is about to vanish."""
        if not device or "://" in device:
            return False
        try:
            for p in self.list_ports():
                if p.get("device") == device:
                    return console_wants_dtr(p.get("vid"), p.get("pid"), interpreter)
        except Exception:
            pass
        return False

    def hard_reset(self) -> dict[str, Any]:
        with self._lock:
            t = self._require()
            interpreter = (self.interpreter or "micropython").lower()
            code = self._HARD_RESET_CODE.get(
                interpreter, self._HARD_RESET_CODE["micropython"]
            )
            device = self.device
            console_dtr = self._console_dtr_for(device, interpreter)
            self._stop_repl_reader()
            try:
                self._take_control_resilient(t, clean=False, timeout_overall=15.0)
            except Exception:
                pass
            t = self.transport
            try:
                if t is not None:
                    if not t.in_raw_repl:
                        t.enter_raw_repl(soft_reset=False, timeout_overall=5)
                    t.exec_raw_no_follow(code.encode())
            except Exception:
                pass
            # Board will reboot; always dispose the handle (do not leave it wedged).
            self._force_close_transport(graceful=False)
            return {
                "ok": True,
                "device": device,
                "runs_main": True,
                "interpreter": interpreter,
                # For hard-reset --monitor: raise DTR on the capture (native CDC).
                "console_dtr": console_dtr,
                "main_skipped": False,
                "note": "device resetting; reconnect required",
            }

    # Entering the bootloader is interpreter-specific. MicroPython exposes
    # machine.bootloader(); CircuitPython has no machine module at all and arms
    # the mode for the next reset instead. Sending the MicroPython form to a
    # CircuitPython board raises ImportError inside exec_raw_no_follow, whose
    # result is never read -- so the board stayed put while the caller was told
    # it had worked.
    _BOOTLOADER_CODE = {
        "micropython": "import time, machine; time.sleep_ms(100); machine.bootloader()",
        "circuitpython": (
            "import time, microcontroller; time.sleep(0.1); "
            "microcontroller.on_next_reset(microcontroller.RunMode.BOOTLOADER); "
            "microcontroller.reset()"
        ),
    }

    def bootloader(self) -> dict[str, Any]:
        interpreter = (self.interpreter or "").lower()
        code = self._BOOTLOADER_CODE.get(interpreter)
        if code is None:
            return {
                "ok": False,
                "error": "cannot enter bootloader: interpreter is %r" % (interpreter or None),
                "hint": "connect first so the interpreter is detected",
            }
        with self._lock:
            t = self._require()
            self._stop_repl_reader()
            try:
                self._take_control_resilient(t, clean=False, timeout_overall=15.0)
            except Exception:
                pass
            t = self.transport
            sent = False
            error: Optional[str] = None
            try:
                if t is not None:
                    if not t.in_raw_repl:
                        t.enter_raw_repl(soft_reset=False, timeout_overall=5)
                    t.exec_raw_no_follow(code.encode())
                    sent = True
                else:
                    error = "no transport"
            except Exception as exc:
                error = "%s: %s" % (type(exc).__name__, exc)
            self._force_close_transport(graceful=False)
            if not sent:
                return {
                    "ok": False,
                    "interpreter": interpreter,
                    "error": error or "could not reach the REPL",
                    "hint": (
                        "a board busy in a tight loop may not accept Ctrl-C; on UF2 boards "
                        "opening the port at 1200 baud with DTR low enters the bootloader "
                        "from the USB stack instead"
                    ),
                }
            return {
                "ok": True,
                "interpreter": interpreter,
                "note": (
                    "device resetting into the bootloader; it re-enumerates as a UF2 "
                    "volume (CIRCUITPY disappears on CircuitPython boards)"
                ),
            }

    def rtc_get(self) -> dict[str, Any]:
        """The board clock in machine.RTC().datetime()'s 8-tuple order.

        CircuitPython's struct_time is reordered to match, so both
        interpreters answer (year, month, day, weekday, hour, minute,
        second, subsecond).
        """

        def op(t):
            t.exec(
                "def _mpftp_rtc():\n"
                " try:\n"
                "  import machine\n"
                " except ImportError:\n"
                "  import rtc\n"
                "  d = rtc.RTC().datetime\n"
                "  return (d[0], d[1], d[2], d[6], d[3], d[4], d[5], 0)\n"
                " return machine.RTC().datetime()\n"
            )
            out = t.eval("_mpftp_rtc()")
            return {"datetime": list(out) if isinstance(out, (tuple, list)) else repr(out)}

        return self.with_raw(op)

    def rtc_set(self) -> dict[str, Any]:
        def op(t):
            return {"datetime": self._apply_rtc(t, force=True)}

        return self.with_raw(op)

    def mip_install(
        self,
        packages: list[str],
        target: Optional[str] = None,
        mpy: bool = True,
        index: Optional[str] = None,
    ) -> dict[str, Any]:
        """Host-side mip install via mpremote (downloads on host, writes to board).

        Defaults ``target`` to ``/lib`` so packages do not land in ``/``.
        """
        if (self.interpreter or "micropython") == "circuitpython":
            raise RuntimeError(
                "mip is MicroPython-only; use circup_install / mpftp circup on CircuitPython"
            )
        from mpremote import mip as mp_mip

        with self._lock:
            was_repl = self._repl_mode
            t = self._require()
            self._stop_repl_reader()
            try:
                t = self._take_control_resilient(t, clean=False, timeout_overall=20.0)
                pkg_index = (index or mp_mip._PACKAGE_INDEX).rstrip("/")
                resolved_target = (target or "/lib").rstrip("/") or "/lib"
                if not resolved_target.startswith("/"):
                    raise RuntimeError(
                        f"mip target must be an absolute board path (got {resolved_target!r})"
                    )

                logs: list[str] = []
                installed: list[str] = []
                for package in packages:
                    version = None
                    pkg = package
                    if "@" in pkg:
                        pkg, version = pkg.split("@", 1)
                    _notify(
                        "mip_progress",
                        {"package": package, "target": resolved_target, "phase": "start"},
                    )
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        print(f"Install {package} -> {resolved_target}")
                        mp_mip._install_package(
                            t, pkg, pkg_index, resolved_target, version, mpy
                        )
                        print("Done")
                    text = buf.getvalue().strip()
                    logs.append(text)
                    installed.append(package)
                    _notify(
                        "mip_progress",
                        {
                            "package": package,
                            "target": resolved_target,
                            "phase": "done",
                            "log": text[-500:],
                        },
                    )
                return {
                    "output": "\n".join(logs),
                    "packages": installed,
                    "target": resolved_target,
                    "index": pkg_index,
                    "mpy": mpy,
                }
            except Exception as e:
                if is_eof_timeout_error(e) or is_dead_serial_error(e):
                    self._release_dead_transport(str(e))
                    raise RuntimeError(
                        f"mip install failed and serial handle was released: {e}"
                    ) from e
                raise
            finally:
                if self.transport and (was_repl or self._repl_mode):
                    self._repl_mode = True
                    self._restore_repl_if_wanted()
                elif not self.transport:
                    self._repl_mode = False

    def _find_circuitpy_host_roots(self) -> list[str]:
        """Host paths for a mounted CIRCUITPY drive (Windows letters or /media)."""
        roots: list[str] = []
        # Windows: query volume label via PowerShell (works from WSL too).
        try:
            ps = (
                "Get-CimInstance Win32_LogicalDisk | "
                "Where-Object { $_.VolumeName -eq 'CIRCUITPY' } | "
                "ForEach-Object { $_.DeviceID }"
            )
            out = subprocess.check_output(
                ["powershell.exe", "-NoProfile", "-Command", ps],
                text=True,
                timeout=15,
                stderr=subprocess.DEVNULL,
            )
            for line in out.splitlines():
                letter = line.strip().rstrip("\\/")
                if len(letter) >= 2 and letter[1] == ":":
                    # Prefer WSL mount if present, else Windows path for Windows python.
                    wsl = f"/mnt/{letter[0].lower()}"
                    if Path(wsl).is_dir():
                        roots.append(wsl)
                    else:
                        roots.append(letter + "\\")
        except Exception:
            pass
        # Linux / macOS common mounts
        for base in ("/media", "/Volumes", str(Path.home() / "media")):
            try:
                p = Path(base)
                if not p.is_dir():
                    continue
                for child in p.iterdir():
                    if child.name.upper() == "CIRCUITPY" and child.is_dir():
                        roots.append(str(child))
            except Exception:
                pass
        # Dedupe preserve order
        seen: set[str] = set()
        uniq: list[str] = []
        for r in roots:
            if r not in seen:
                seen.add(r)
                uniq.append(r)
        return uniq

    def _push_lib_tree_host(self, lib_src: Path, dest_lib: Path) -> list[str]:
        """Copy staged lib/ contents onto a host CIRCUITPY/lib tree."""
        dest_lib.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        for child in sorted(lib_src.iterdir(), key=lambda p: p.name.lower()):
            target = dest_lib / child.name
            if child.is_dir():
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(child, target)
            else:
                shutil.copy2(child, target)
            copied.append(str(target))
        return copied

    def _circuitpy_msc_root(self) -> Optional[Path]:
        """Host CIRCUITPY mount when interpreter is CircuitPython and the drive is up.

        While USB MSC is exposed, the board FS is read-only over serial/Web.
        Host writes to this volume are the default CircuitPython file workflow
        (same idea as Mu/Thonny/drag-drop and circup ``--path``).
        """
        if (self.interpreter or "") != "circuitpython":
            return None
        for root in self._find_circuitpy_host_roots():
            p = Path(root)
            try:
                if p.is_dir():
                    return p
            except OSError:
                continue
        return None

    def _circuitpy_host_path(self, remote: str) -> Optional[Path]:
        root = self._circuitpy_msc_root()
        if root is None:
            return None
        return map_circuitpy_remote_path(root, remote)

    def _ensure_cp_writable(self, t: Any) -> None:
        """Remount CIRCUITPY root read-write when USB MSC left it read-only."""
        if (self.interpreter or "") != "circuitpython":
            return
        try:
            t.exec(
                "try:\n"
                " import storage\n"
                " storage.remount('/', False)\n"
                "except Exception:\n"
                " pass\n"
            )
        except Exception:
            pass

    def _resolve_circup_exe(self) -> tuple[str, bool]:
        """Return (circup_executable_or_python, use_module_invocation)."""
        circup_exe = shutil.which("circup") or shutil.which("circup.exe")
        if circup_exe:
            return circup_exe, False
        try:
            import circup as _circup  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "circup is not installed for the sidecar Python. "
                "Install with: python -m pip install circup "
                "(on WSL use the Windows python.exe that runs the sidecar)"
            ) from e
        return sys.executable, True

    def _run_circup(self, argv: list[str], *, use_module: bool = False) -> str:
        del use_module  # argv is fully formed by callers
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=600)
        out_text = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        if proc.returncode != 0:
            raise RuntimeError(
                f"circup failed (exit {proc.returncode}): {out_text.strip() or 'no output'}"
            )
        return out_text.strip()

    def _cp_web_workflow_info(self, t: Any) -> dict[str, Any]:
        """Read Wi-Fi IP + web API password from the board (no secrets logged)."""
        code = r"""
_host = None
_pwd = None
_ssid = None
try:
 import os as _os
 _pwd = _os.getenv('CIRCUITPY_WEB_API_PASSWORD')
 _ssid = _os.getenv('CIRCUITPY_WIFI_SSID')
except Exception:
 pass
try:
 import wifi
 _ip = wifi.radio.ipv4_address
 if _ip is not None:
  _host = str(_ip)
except Exception:
 pass
print(repr((_host, bool(_pwd), _ssid)))
"""
        out = t.exec(code)
        text = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
        line = text.strip().splitlines()[-1] if text.strip() else "(None, False, None)"
        import ast

        host, has_pwd, ssid = ast.literal_eval(line)
        password = None
        if has_pwd:
            # Fetch password in a second eval so we can avoid printing it in exec stdout
            # that might land in logs; still needed for circup argv.
            try:
                t.exec("import os")
                password = t.eval("os.getenv('CIRCUITPY_WEB_API_PASSWORD')")
                if password is not None:
                    password = str(password)
            except Exception:
                password = None
        env_pwd = os.environ.get("CIRCUP_WEBWORKFLOW_PASSWORD") or os.environ.get(
            "MPFTP_CIRCUITPY_WEB_PASSWORD"
        )
        if env_pwd:
            password = env_pwd
        return {
            "host": host,
            "password": password,
            "ssid": ssid,
            "ready": bool(host and password),
        }

    def _cp_web_remount_writable(self, host: str, password: str) -> bool:
        """Ask Web Workflow to remount the FS writable to the device."""
        import urllib.error
        import urllib.request

        url = f"http://{host}/fs/"
        req = urllib.request.Request(
            url,
            data=b'{"writable":true}',
            method="PUT",
            headers={"Content-Type": "application/json"},
        )
        # Empty username, password as web API password (Basic auth).
        import base64 as _b64

        token = _b64.b64encode(f":{password}".encode()).decode("ascii")
        req.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", "replace")
            return "USB storage active" not in body
        except Exception:
            return False

    def _eject_circuitpy_host(self) -> bool:
        """Best-effort eject of CIRCUITPY so web workflow can write."""
        try:
            ps = r"""
$shell = New-Object -ComObject Shell.Application
$usb = $shell.NameSpace(17)
$n = 0
foreach ($item in $usb.Items()) {
  if ($item.Name -match 'CIRCUITPY') {
    $item.InvokeVerb('Eject')
    $n++
  }
}
Write-Output $n
"""
            out = subprocess.check_output(
                ["powershell.exe", "-NoProfile", "-Command", ps],
                text=True,
                timeout=20,
                stderr=subprocess.DEVNULL,
            )
            return int((out or "0").strip().splitlines()[-1] or "0") > 0
        except Exception:
            return False

    def _cp_prepare_web_writable(self, t: Any, host: str, password: str) -> bool:
        """Eject CIRCUITPY on the host and remount writable for Web Workflow.

        Returns True if ``/fs/`` reports writable. Host eject alone is often not
        enough while the USB MSC *interface* is still enabled in firmware
        (CircuitPython keeps the FS read-only until ``storage.disable_usb_drive()``
        in boot.py, or USB is unplugged). We still try eject + remount for the
        cases where the host held the volume open.
        """
        import time as _time

        self._eject_circuitpy_host()
        _time.sleep(0.8)
        self._ensure_cp_writable(t)
        self._cp_web_remount_writable(host, password)
        try:
            import base64 as _b64
            import json as _json
            import urllib.request

            req = urllib.request.Request(
                f"http://{host}/fs/",
                headers={
                    "Accept": "application/json",
                    "Authorization": "Basic "
                    + _b64.b64encode(f":{password}".encode()).decode("ascii"),
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read().decode("utf-8", "replace"))
            return bool(data.get("writable"))
        except Exception:
            return False

    def circup_install(
        self,
        packages: list[str],
        *,
        py: bool = False,
        target: str = "/lib",
        host: Optional[str] = None,
        password: Optional[str] = None,
        prefer_web: bool = True,
    ) -> dict[str, Any]:
        """Install packages via circup.

        Preference order (no board ``boot.py`` required):

        1. **CIRCUITPY mount** on the host — ``circup --path`` (fast USB disk).
        2. **Web Workflow** — ``circup --host`` when Wi‑Fi is up *and* the FS is
           device-writable (USB MSC not locking it).
        3. Host stage + serial put, then MSC copy fallback.
        """
        if (self.interpreter or "micropython") != "circuitpython":
            raise RuntimeError(
                "circup is CircuitPython-only; use mip_install / mpftp mip on MicroPython"
            )
        if not packages:
            raise RuntimeError("circup_install: packages required")

        circup_exe, use_module = self._resolve_circup_exe()

        with self._lock:
            t = self._require()
            self._stop_repl_reader()
            if not t.in_raw_repl:
                t.enter_raw_repl(soft_reset=False)
            try:
                cpy_version, board_id = self._cp_version_and_board(t)
                web = self._cp_web_workflow_info(t)
                web_host = host or web.get("host")
                web_password = password or web.get("password")
                web_fallback_err: Optional[BaseException] = None

                # --- 1. Prefer mounted CIRCUITPY (no board edits needed) ---
                roots = self._find_circuitpy_host_roots()
                if roots:
                    root = roots[0]
                    # Prefer a Windows drive path for circup.exe under WSL.
                    path_for_circup = root
                    if root.startswith("/mnt/") and len(root) >= 6:
                        path_for_circup = f"{root[5].upper()}:\\"
                    try:
                        if use_module:
                            argv = [
                                circup_exe,
                                "-c",
                                (
                                    "import sys; from circup import main; "
                                    "sys.argv=['circup']+sys.argv[1:]; "
                                    "raise SystemExit(main())"
                                ),
                                "--path",
                                path_for_circup,
                                "install",
                            ]
                        else:
                            argv = [circup_exe, "--path", path_for_circup, "install"]
                        if py:
                            argv.append("--py")
                        argv.extend(packages)
                        out_text = self._run_circup(argv, use_module=use_module)
                        return {
                            "output": out_text,
                            "packages": list(packages),
                            "target": target if target.startswith("/") else "/" + target,
                            "cpy_version": cpy_version,
                            "board_id": board_id,
                            "py": py,
                            "transport": "circuitpy-path",
                            "path": path_for_circup,
                        }
                    except Exception as e:
                        web_fallback_err = e  # try web / serial next

                # --- 2. Web Workflow when FS is device-writable ---
                if prefer_web and web_host and web_password:
                    if self._cp_prepare_web_writable(t, str(web_host), str(web_password)):
                        if use_module:
                            argv = [
                                circup_exe,
                                "-c",
                                (
                                    "import sys; from circup import main; "
                                    "sys.argv=['circup']+sys.argv[1:]; "
                                    "raise SystemExit(main())"
                                ),
                                "--host",
                                str(web_host),
                                "--password",
                                str(web_password),
                                "install",
                            ]
                            if py:
                                argv.append("--py")
                            argv.extend(packages)
                        else:
                            argv = build_circup_web_argv(
                                circup_exe=circup_exe,
                                host=str(web_host),
                                password=str(web_password),
                                packages=packages,
                                py=py,
                            )
                        try:
                            out_text = self._run_circup(argv, use_module=use_module)
                            return {
                                "output": out_text,
                                "packages": list(packages),
                                "target": target if target.startswith("/") else "/" + target,
                                "cpy_version": cpy_version,
                                "board_id": board_id,
                                "py": py,
                                "transport": "web-workflow",
                                "host": str(web_host),
                            }
                        except Exception as web_err:
                            web_fallback_err = web_err
                    else:
                        web_fallback_err = RuntimeError(
                            "Web Workflow FS is not writable while USB mass storage "
                            "is active. Mount CIRCUITPY and mpftp will use circup "
                            "--path, or unplug USB / disable MSC in boot.py for "
                            "Wi-Fi installs."
                        )

                # --- 3. Fallback: stage on host, serial put / MSC ---
                self._ensure_cp_writable(t)
                stage = tempfile.mkdtemp(prefix="mpftp-circup-")
                try:
                    boot = Path(stage) / "boot_out.txt"
                    boot.write_text(
                        circup_boot_out_text(cpy_version=cpy_version, board_id=board_id),
                        encoding="utf-8",
                    )
                    (Path(stage) / "lib").mkdir(exist_ok=True)
                    stage_for_circup = stage
                    if sys.platform == "win32" or (
                        os.name == "nt"
                        or (
                            os.environ.get("WSL_DISTRO_NAME")
                            and str(circup_exe).lower().endswith(".exe")
                        )
                    ):
                        try:
                            win = subprocess.check_output(
                                ["wslpath", "-w", stage], text=True
                            ).strip()
                            if win:
                                stage_for_circup = win
                        except Exception:
                            pass

                    if use_module:
                        argv = [
                            circup_exe,
                            "-c",
                            (
                                "import sys; from circup import main; "
                                "sys.argv=['circup']+sys.argv[1:]; raise SystemExit(main())"
                            ),
                            "--path",
                            stage_for_circup,
                            "--board-id",
                            board_id,
                            "--cpy-version",
                            cpy_version,
                            "install",
                        ]
                        if py:
                            argv.append("--py")
                        argv.extend(packages)
                    else:
                        argv = build_circup_argv(
                            circup_exe=circup_exe,
                            stage_path=stage_for_circup,
                            packages=packages,
                            cpy_version=cpy_version,
                            board_id=board_id,
                            py=py,
                        )
                    out_text = self._run_circup(argv, use_module=use_module)

                    lib_src = Path(stage) / "lib"
                    if not lib_src.is_dir():
                        raise RuntimeError("circup did not create a lib/ staging directory")
                    dest_root = target if target.startswith("/") else "/" + target
                    copied: list[str] = []
                    transport = "serial"
                    serial_err: Optional[BaseException] = None
                    try:
                        self._ensure_cp_writable(t)
                        try:
                            t.fs_mkdir(dest_root if dest_root != "/" else "/lib")
                        except Exception:
                            pass
                        for child in sorted(lib_src.iterdir(), key=lambda p: p.name.lower()):
                            remote = dest_root.rstrip("/") + "/" + child.name
                            self._cp_local_to_remote(
                                t,
                                str(child),
                                remote,
                                verify=False,
                                copied=copied,
                                verified=[],
                            )
                        self._sync_remote_fs(t)
                    except Exception as e:
                        serial_err = e
                        err_s = str(e).lower()
                        if (
                            "read-only" not in err_s
                            and "errno 30" not in err_s
                            and "erofs" not in err_s
                        ):
                            raise
                        roots = self._find_circuitpy_host_roots()
                        if not roots:
                            hint = ""
                            if web_fallback_err is not None:
                                hint = f" ({web_fallback_err})"
                            raise RuntimeError(
                                "circup staged libraries, but the CircuitPython filesystem "
                                "is read-only over serial and no CIRCUITPY drive is mounted."
                                + hint
                                + f" Serial error: {e}"
                            ) from e
                        host_lib = Path(roots[0]) / "lib"
                        host_root = roots[0]
                        if host_root.startswith("/mnt/") and len(host_root) >= 6:
                            letter = host_root[5].upper()
                            win_lib = Path(f"{letter}:/lib")
                            try:
                                win_lib.mkdir(parents=True, exist_ok=True)
                                host_lib = win_lib
                            except Exception:
                                pass
                        copied = self._push_lib_tree_host(lib_src, host_lib)
                        transport = "circuitpy-msc"
                    result = {
                        "output": out_text,
                        "packages": list(packages),
                        "target": dest_root,
                        "cpy_version": cpy_version,
                        "board_id": board_id,
                        "py": py,
                        "files": len(copied),
                        "transport": transport,
                        "serial_error": str(serial_err) if serial_err else None,
                    }
                    if web_fallback_err is not None:
                        result["web_workflow_error"] = str(web_fallback_err)
                    return result
                finally:
                    shutil.rmtree(stage, ignore_errors=True)
            finally:
                if self._repl_mode:
                    try:
                        if self.transport and self.transport.in_raw_repl:
                            self.transport.exit_raw_repl()
                    except Exception:
                        pass
                    self._start_repl_reader()

    def _cp_version_and_board(self, t: Any) -> tuple[str, str]:
        """Best-effort CircuitPython version + board id for circup."""
        code = r"""
import sys
_ver = '.'.join(str(x) for x in sys.implementation.version[:3])
_bid = 'unknown'
try:
 _t = open('/boot_out.txt').read()
 for _line in _t.split('\n'):
  if _line.startswith('Board ID:'):
   _bid = _line.split(':',1)[1].strip() or _bid
   break
except Exception:
 pass
if _bid == 'unknown':
 try:
  import os as _os
  _u = _os.uname()
  _bid = getattr(_u, 'machine', 'unknown') or 'unknown'
 except Exception:
  pass
print(repr((_ver, _bid)))
"""
        out = t.exec(code)
        text = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
        line = text.strip().splitlines()[-1] if text.strip() else "('9.0.0', 'unknown')"
        import ast

        ver, bid = ast.literal_eval(line)
        return str(ver), str(bid)

    def df(self) -> dict[str, Any]:
        # Prefer vfs.mount enrichment (MicroPython); fall back to os.statvfs only.
        code = r"""
import os
rows=[]
_ms=[]
try:
 import vfs
 _ms=list(vfs.mount())
except Exception:
 _ms=[]
if not _ms:
 try:
  _ms=[('<root>', '/')]
 except Exception:
  _ms=[]
for _v,_p in _ms:
 try:
  _s=os.statvfs(_p)
 except Exception:
  continue
 _sz=_s[0]*_s[2]
 _av=_s[0]*_s[3]
 rows.append({'fs':str(_v),'size':_sz,'used':_sz-_av,'avail':_av,'mounted':_p})
if not rows:
 try:
  _s=os.statvfs('/')
  _sz=_s[0]*_s[2]
  _av=_s[0]*_s[3]
  rows.append({'fs':'/','size':_sz,'used':_sz-_av,'avail':_av,'mounted':'/'})
 except Exception:
  pass
print(repr(rows))
"""

        def op(t):
            out = t.exec(code)
            text = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
            line = text.strip().splitlines()[-1] if text.strip() else "[]"
            import ast

            rows = ast.literal_eval(line)
            return {"mounts": rows}

        return self.with_raw(op)

    def mount(self, path: str, unsafe_links: bool = False) -> dict[str, Any]:
        self._require_micropython("mount")
        if self.is_ble:
            raise RuntimeError(
                "mount works over serial only. Over BLE every file the board opens from "
                "/remote would cross the REPL a byte at a time. Use put/get instead."
            )
        if self.is_network:
            raise RuntimeError(
                "mount works over serial only. Over WebREPL every file the board "
                "opens from /remote crawls through the REPL a byte at a time: a "
                "directory listing took 8 s and a 27 KB read never finished. "
                "Use put/get (or File Transfer) over Wi-Fi instead."
            )
        with self._lock:
            t = self._require()
            self._stop_repl_reader()
            if not t.in_raw_repl:
                t.enter_raw_repl(soft_reset=False)
            t.mount_local(path, unsafe_links=unsafe_links)
            self._mounted_path = path
            return {"path": path, "mount": getattr(t, "fs_hook_mount", "/remote")}

    def umount(self) -> dict[str, Any]:
        self._require_micropython("umount")
        with self._lock:
            t = self._require()
            self._stop_repl_reader()
            if not t.in_raw_repl:
                t.enter_raw_repl(soft_reset=False)
            t.umount_local()
            self._mounted_path = None
            if self._repl_mode:
                t.exit_raw_repl()
                self._start_repl_reader()
            return {"ok": True}

    def _mp_state(self):
        """Minimal mpremote State shim for romfs helpers."""
        t = self._require()

        class _State:
            def __init__(self, transport):
                self.transport = transport

            def ensure_raw_repl(self):
                if not self.transport.in_raw_repl:
                    self.transport.enter_raw_repl(soft_reset=False)

            def did_action(self):
                pass

        return _State(t)

    def romfs_query(self) -> dict[str, Any]:
        self._require_micropython("romfs")
        from mpremote import commands as mp_cmd

        with self._lock:
            self._stop_repl_reader()
            state = self._mp_state()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                mp_cmd._do_romfs_query(state, argparse.Namespace())
            if self._repl_mode:
                try:
                    if self.transport and self.transport.in_raw_repl:
                        self.transport.exit_raw_repl()
                except Exception:
                    pass
                self._start_repl_reader()
            return {"output": buf.getvalue().strip()}

    def romfs_build(
        self, path: str, output: Optional[str] = None, mpy: bool = True
    ) -> dict[str, Any]:
        self._require_micropython("romfs")
        from mpremote import commands as mp_cmd

        # Host-only: mpremote still calls state.did_action() before make_romfs.
        class _HostState:
            def did_action(self):
                pass

        args = argparse.Namespace(path=path, output=output, mpy=mpy)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mp_cmd._do_romfs_build(_HostState(), args)
        out_file = output or (path + ".romfs")
        size = Path(out_file).stat().st_size if Path(out_file).is_file() else 0
        return {"output": buf.getvalue().strip(), "output_file": out_file, "size": size}

    def romfs_deploy(
        self, path: str, partition: int = 0, mpy: bool = True
    ) -> dict[str, Any]:
        self._require_micropython("romfs")
        from mpremote import commands as mp_cmd

        with self._lock:
            self._stop_repl_reader()
            state = self._mp_state()
            args = argparse.Namespace(path=path, partition=partition, mpy=mpy)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                mp_cmd._do_romfs_deploy(state, args)
            if self._repl_mode:
                try:
                    if self.transport and self.transport.in_raw_repl:
                        self.transport.exit_raw_repl()
                except Exception:
                    pass
                self._start_repl_reader()
            return {"output": buf.getvalue().strip(), "path": path, "partition": partition}

    def fs_cp(self, src: str, dest: str, verify: bool = False, mpy: bool = False) -> dict[str, Any]:
        """
        Recursive copy using mpremote ':' prefix for board paths.
        Examples: local→board  ./a.py :/a.py
                  board→local  :/a.py ./a.py
                  board→board  :/a.py :/b.py
        """
        src_remote, src_path = split_fs_path(src)
        dest_remote, dest_path = split_fs_path(dest)
        copied: list[str] = []
        verified: list[str] = []

        def op(t):
            nonlocal copied, verified
            self._ensure_cp_writable(t)
            if src_remote and dest_remote:
                self._cp_remote_to_remote(t, src_path, dest_path, verify, copied, verified)
            elif not src_remote and dest_remote:
                self._cp_local_to_remote(t, src_path, dest_path, verify, copied, verified, mpy=mpy)
            elif src_remote and not dest_remote:
                self._cp_remote_to_local(t, src_path, dest_path, verify, copied, verified)
            else:
                raise RuntimeError("fs_cp: at least one path must be a board path (prefix with :)")
            return {
                "src": src,
                "dest": dest,
                "files": len(copied),
                "copied": copied,
                "verified": verified if verify else None,
            }

        return self.with_raw(self._transfer(op))

    def _remote_isdir(self, t, path: str) -> bool:
        try:
            st = t.fs_stat(path)
            return bool(st.st_mode & 0x4000)
        except Exception:
            return False

    def _cp_local_to_remote(
        self,
        t,
        src: str,
        dest: str,
        verify: bool,
        copied: list[str],
        verified: list[str],
        mpy: bool = False,
    ) -> None:
        src_p = Path(src)
        if not src_p.exists():
            raise RuntimeError(f"local path not found: {src}")

        msc_root = self._circuitpy_msc_root()
        if msc_root is not None:
            if mpy:
                raise RuntimeError(
                    "compile-on-upload is MicroPython-only (connected interpreter is circuitpython)"
                )
            self._cp_local_to_circuitpy_msc(
                msc_root, src_p, dest, verify, copied, verified
            )
            return

        mpy_exe = self._prepare_mpy_compile(t) if mpy else None
        mpy_exclude = set(config.resolve("mpyExcludeFiles")) if mpy_exe else set()

        def stage(name: str, data: bytes) -> tuple[str, bytes]:
            if mpy_exe and name.endswith(".py") and name not in mpy_exclude:
                return self._compile_to_mpy(mpy_exe, name, data)
            return name, data

        if src_p.is_dir():
            # If dest exists as dir, copy into it as basename; else create dest as the tree root.
            dest_exists = False
            try:
                dest_exists = self._remote_isdir(t, dest)
            except Exception:
                dest_exists = False
            root = (dest.rstrip("/") + "/" + src_p.name) if dest_exists else dest
            try:
                t.fs_mkdir(root)
            except Exception:
                pass
            for dirpath, dirnames, filenames in os.walk(src):
                # Prune ignored dirs in-place so os.walk does not descend into them.
                dirnames[:] = [d for d in dirnames if not should_skip_transfer_entry(d)]
                rel = os.path.relpath(dirpath, src)
                remote_dir = root if rel == "." else root.rstrip("/") + "/" + rel.replace("\\", "/")
                if rel != ".":
                    try:
                        t.fs_mkdir(remote_dir)
                    except Exception:
                        pass
                for name in dirnames:
                    try:
                        t.fs_mkdir(remote_dir.rstrip("/") + "/" + name)
                    except Exception:
                        pass
                for name in filenames:
                    if should_skip_transfer_entry(name):
                        continue
                    local_file = Path(dirpath) / name
                    data = local_file.read_bytes()
                    out_name, data = stage(name, data)
                    remote_file = remote_dir.rstrip("/") + "/" + out_name
                    t.fs_writefile(remote_file, data)
                    copied.append(remote_file)
                    if verify:
                        digest = t.fs_hashfile(remote_file, "sha256")
                        hx = digest.hex() if isinstance(digest, bytes) else str(digest)
                        if hx != host_sha256(data):
                            raise RuntimeError(f"hash mismatch after upload: {remote_file}")
                        verified.append(remote_file)
        else:
            # File copy: if dest is/exists as directory, place basename inside.
            final = dest
            if dest.endswith("/") or self._remote_isdir(t, dest):
                final = dest.rstrip("/") + "/" + src_p.name
            data = src_p.read_bytes()
            out_name, data = stage(src_p.name, data)
            if out_name != src_p.name:
                final = _replace_remote_basename(final, out_name)
            t.fs_writefile(final, data)
            copied.append(final)
            if verify:
                digest = t.fs_hashfile(final, "sha256")
                hx = digest.hex() if isinstance(digest, bytes) else str(digest)
                if hx != host_sha256(data):
                    raise RuntimeError(f"hash mismatch after upload: {final}")
                verified.append(final)
        if copied:
            self._sync_remote_fs(t)

    def _cp_local_to_circuitpy_msc(
        self,
        msc_root: Path,
        src_p: Path,
        dest: str,
        verify: bool,
        copied: list[str],
        verified: list[str],
    ) -> None:
        """Local → board copy via mounted CIRCUITPY (USB MSC)."""

        def _host_isdir(remote: str) -> bool:
            p = map_circuitpy_remote_path(msc_root, remote)
            try:
                return p.is_dir()
            except OSError:
                return False

        def _write_one(local_file: Path, remote_file: str) -> None:
            host = map_circuitpy_remote_path(msc_root, remote_file)
            host.parent.mkdir(parents=True, exist_ok=True)
            data = local_file.read_bytes()
            host.write_bytes(data)
            copied.append(remote_file)
            if verify:
                if host_sha256(host.read_bytes()) != host_sha256(data):
                    raise RuntimeError(f"hash mismatch after MSC upload: {remote_file}")
                verified.append(remote_file)

        if src_p.is_dir():
            dest_exists = _host_isdir(dest)
            root = (dest.rstrip("/") + "/" + src_p.name) if dest_exists else dest
            map_circuitpy_remote_path(msc_root, root).mkdir(parents=True, exist_ok=True)
            for dirpath, dirnames, filenames in os.walk(src_p):
                dirnames[:] = [d for d in dirnames if not should_skip_transfer_entry(d)]
                rel = os.path.relpath(dirpath, src_p)
                remote_dir = root if rel == "." else root.rstrip("/") + "/" + rel.replace("\\", "/")
                map_circuitpy_remote_path(msc_root, remote_dir).mkdir(parents=True, exist_ok=True)
                for name in dirnames:
                    map_circuitpy_remote_path(
                        msc_root, remote_dir.rstrip("/") + "/" + name
                    ).mkdir(parents=True, exist_ok=True)
                for name in filenames:
                    if should_skip_transfer_entry(name):
                        continue
                    local_file = Path(dirpath) / name
                    remote_file = remote_dir.rstrip("/") + "/" + name
                    _write_one(local_file, remote_file)
        else:
            final = dest
            if dest.endswith("/") or _host_isdir(dest):
                final = dest.rstrip("/") + "/" + src_p.name
            _write_one(src_p, final)

    def _cp_remote_to_local(
        self, t, src: str, dest: str, verify: bool, copied: list[str], verified: list[str]
    ) -> None:
        dest_p = Path(dest)
        if self._remote_isdir(t, src):
            root = dest_p / Path(src).name if dest_p.exists() and dest_p.is_dir() else dest_p
            root.mkdir(parents=True, exist_ok=True)

            def walk(remote: str, local: Path) -> None:
                for e in self._board_listdir(t, remote if remote != "/" else ""):
                    name = e.name
                    if should_skip_transfer_entry(name):
                        continue
                    rpath = (remote.rstrip("/") + "/" + name) if remote != "/" else "/" + name
                    lpath = local / name
                    if e.st_mode & 0x4000:
                        lpath.mkdir(parents=True, exist_ok=True)
                        walk(rpath, lpath)
                    else:
                        data = t.fs_readfile(rpath)
                        lpath.write_bytes(data)
                        copied.append(str(lpath))
                        if verify:
                            digest = t.fs_hashfile(rpath, "sha256")
                            hx = digest.hex() if isinstance(digest, bytes) else str(digest)
                            if hx != host_sha256(data):
                                raise RuntimeError(f"hash mismatch after download: {rpath}")
                            verified.append(str(lpath))

            walk(src, root)
        else:
            data = t.fs_readfile(src)
            if dest_p.exists() and dest_p.is_dir() or str(dest).endswith(("/", "\\")):
                dest_p = dest_p / Path(src).name
            dest_p.parent.mkdir(parents=True, exist_ok=True)
            dest_p.write_bytes(data)
            copied.append(str(dest_p))
            if verify:
                digest = t.fs_hashfile(src, "sha256")
                hx = digest.hex() if isinstance(digest, bytes) else str(digest)
                if hx != host_sha256(data):
                    raise RuntimeError(f"hash mismatch after download: {src}")
                verified.append(str(dest_p))

    def _cp_remote_to_remote(
        self, t, src: str, dest: str, verify: bool, copied: list[str], verified: list[str]
    ) -> None:
        if self._remote_isdir(t, src):
            root = dest.rstrip("/") + "/" + Path(src).name if self._remote_isdir(t, dest) else dest
            try:
                t.fs_mkdir(root)
            except Exception:
                pass

            def walk(remote: str, dest_dir: str) -> None:
                for e in self._board_listdir(t, remote if remote != "/" else ""):
                    name = e.name
                    if should_skip_transfer_entry(name):
                        continue
                    rpath = (remote.rstrip("/") + "/" + name) if remote != "/" else "/" + name
                    dpath = dest_dir.rstrip("/") + "/" + name
                    if e.st_mode & 0x4000:
                        try:
                            t.fs_mkdir(dpath)
                        except Exception:
                            pass
                        walk(rpath, dpath)
                    else:
                        data = t.fs_readfile(rpath)
                        t.fs_writefile(dpath, data)
                        copied.append(dpath)
                        if verify:
                            d1 = t.fs_hashfile(rpath, "sha256")
                            d2 = t.fs_hashfile(dpath, "sha256")
                            h1 = d1.hex() if isinstance(d1, bytes) else str(d1)
                            h2 = d2.hex() if isinstance(d2, bytes) else str(d2)
                            if h1 != h2:
                                raise RuntimeError(f"hash mismatch after copy: {dpath}")
                            verified.append(dpath)

            walk(src, root)
        else:
            final = dest
            if dest.endswith("/") or self._remote_isdir(t, dest):
                final = dest.rstrip("/") + "/" + Path(src).name
            data = t.fs_readfile(src)
            t.fs_writefile(final, data)
            copied.append(final)
            if verify:
                d1 = t.fs_hashfile(src, "sha256")
                d2 = t.fs_hashfile(final, "sha256")
                h1 = d1.hex() if isinstance(d1, bytes) else str(d1)
                h2 = d2.hex() if isinstance(d2, bytes) else str(d2)
                if h1 != h2:
                    raise RuntimeError(f"hash mismatch after copy: {final}")
                verified.append(final)

    def edit_pull(self, path: str) -> dict[str, Any]:
        """Read a board file for host-side editing (extension / CLI)."""
        return self.fs_read(path)

    def edit_push(self, path: str, data_b64: str) -> dict[str, Any]:
        return self.fs_write(path, data_b64)

    # --- REPL ---

    def repl_start(self) -> dict[str, Any]:
        with self._lock:
            t = self._require()
            if t.in_raw_repl:
                t.exit_raw_repl()
            self._repl_mode = True
            self._start_repl_reader()
            # Nudge CR so MicroPython/CircuitPython show ">>> " (CLI + UI).
            try:
                t.serial.write(b"\r")
            except Exception as e:
                if is_dead_serial_error(e):
                    self._release_dead_transport(str(e))
                    raise RuntimeError(f"repl_start failed: {e}") from e
            return {"device": self.device}

    def repl_stop(self) -> dict[str, Any]:
        with self._lock:
            self._repl_mode = False
            self._stop_repl_reader()
            return {"ok": True}

    def repl_write(self, data_b64: str) -> dict[str, Any]:
        data = base64.b64decode(data_b64)
        with self._lock:
            t = self._require()
            if t.in_raw_repl:
                t.exit_raw_repl()
            if not self._repl_mode:
                self._repl_mode = True
                self._start_repl_reader()
            serial = t.serial
            before = getattr(serial, "rx_total", None)
            try:
                serial.write(data)
            except Exception as e:
                if is_dead_serial_error(e):
                    self._release_dead_transport(str(e))
                    raise RuntimeError(f"repl_write failed: {e}") from e
                raise
            if (
                b"\x03" in data
                and isinstance(serial, (webrepl.WebSocketSerial, ble.BleSerial))
                and before is not None
            ):
                self._watch_repl_interrupt(serial, before)
            return {"bytes": len(data)}

    def _watch_repl_interrupt(self, serial: Any, before: int) -> None:
        """Ctrl-C typed in a REPL over WebREPL: if the board says nothing back,
        even when poked, tell the terminal it's in a loop that never yields
        (a repl_error)."""

        def pump() -> bool:
            if not serial.is_open:
                return False
            if not (self._repl_thread and self._repl_thread.is_alive()):
                try:
                    serial.inWaiting()  # nobody else is pumping the socket
                except Exception:
                    return False
            return True

        def watch() -> None:
            reply = self._await_network_reply(serial, before, raw=False, pump=pump)
            if reply is None and serial.rx_total == before and serial.is_open:
                _notify("repl_error", {"message": wifiboard.STUCK_MESSAGE})

        threading.Thread(target=watch, daemon=True).start()

    def _start_repl_reader(self) -> None:
        if self._repl_thread and self._repl_thread.is_alive():
            return
        self._repl_stop.clear()
        self._repl_thread = threading.Thread(target=self._repl_reader_loop, daemon=True)
        self._repl_thread.start()

    def _stop_repl_reader(self) -> None:
        self._repl_stop.set()
        th = self._repl_thread
        if th and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=1.0)
        self._repl_thread = None

    def _repl_reader_loop(self) -> None:
        while not self._repl_stop.is_set():
            t = self.transport
            if not t or t.in_raw_repl:
                self._repl_stop.wait(0.05)
                continue
            try:
                n = t.serial.inWaiting()
                if n and not self._lock.acquire(blocking=False):
                    self._repl_stop.wait(0.02)
                    continue
                try:
                    if n:
                        data = t.serial.read(n)
                        if data:
                            _notify(
                                "repl_data",
                                {"data_b64": base64.b64encode(data).decode("ascii")},
                            )
                finally:
                    if n:
                        self._lock.release()
                if not n:
                    self._repl_stop.wait(0.02)
            except Exception as e:
                if is_dead_serial_error(e):
                    self._release_dead_transport(str(e))
                _notify("repl_error", {"message": str(e)})
                break

    def debug_tee_start(
        self,
        device: str,
        baud: int = 115200,
        log_path: Optional[str] = None,
        wait: Optional[float] = None,
        dtr: bool = False,
    ) -> dict[str, Any]:
        """Open a second COM port read-only for debug prints (does not take control).

        ``wait`` is for a port that is going away and coming back, as a
        native-USB board's does across a reset (``hard-reset --monitor``,
        mpftp#60). The open is retried for up to ``wait`` seconds, and if the
        port drops while it's being read, the tee closes it and waits up to
        ``wait`` seconds again for it to reappear rather than stopping. A
        USB-UART bridge (a CH343) never goes away, so it opens at once and
        catches the boot banner.

        ``dtr`` raises DTR (RTS stays low) for a native TinyUSB CDC port,
        which sends nothing without it; see :func:`console_wants_dtr`.
        """
        import time

        device = (device or "").strip()
        if not device:
            raise RuntimeError("debug_tee_start requires device=")
        if self.device and device == self.device:
            raise RuntimeError(
                f"debug tee device {device} is the control port; pick the other USB CDC"
            )
        wait = float(wait) if wait else None
        with self._lock:
            self.debug_tee_stop()
            # Cleared now, not after the open: the open's retry waits on it.
            self._tee_stop.clear()
        path = Path(log_path) if log_path else (_mpftp_dir() / "debug-tee.log")
        path.parent.mkdir(parents=True, exist_ok=True)
        baud = int(baud or 115200)
        started = time.monotonic()
        # Retry outside the lock: waiting for a port must not hold up other RPCs.
        dtr = bool(dtr)
        self._tee_started = started
        ser = self._open_tee_serial(device, baud, wait, dtr)
        with self._lock:
            self._tee_serial = ser
            self._tee_device = device
            self._tee_stop.clear()
            self._tee_thread = threading.Thread(
                target=self._debug_tee_loop,
                args=(path, baud, wait, dtr),
                daemon=True,
            )
            self._tee_thread.start()
            result: dict[str, Any] = {
                "ok": True,
                "device": device,
                "log_path": str(path),
                "baud": baud,
            }
            if dtr:
                result["dtr"] = True
            if wait:
                result["waited_s"] = round(time.monotonic() - started, 2)
            return result

    def _tee_serial_factory(self) -> Any:
        import serial

        return serial.Serial()

    def _open_tee_serial(
        self, device: str, baud: int, wait: Optional[float], dtr: bool = False
    ) -> Any:
        """Open ``device`` read-only, RTS low and DTR low unless ``dtr``;
        retry for ``wait`` seconds."""
        import time

        deadline = time.monotonic() + wait if wait else None
        while True:
            ser = self._tee_serial_factory()
            ser.port = device
            ser.baudrate = baud
            ser.timeout = 0.2
            # Avoid DTR/RTS toggles that reset some ESP boards.
            try:
                ser.dtr = dtr
                ser.rts = False
            except Exception:
                pass
            try:
                ser.open()
                if wait:
                    # Tells a bounded capture to count from here.
                    _notify(
                        "debug_tee_open",
                        {
                            "device": device,
                            "after_s": round(
                                time.monotonic() - self._tee_started, 2
                            ),
                        },
                    )
                return ser
            except Exception as e:
                if deadline is None or time.monotonic() >= deadline:
                    if deadline is not None:
                        raise RuntimeError(
                            f"{device} did not come back within {wait:g} s: {e}"
                        ) from e
                    raise
                if self._tee_stop.wait(0.2):
                    raise RuntimeError(f"stopped waiting for {device}") from e

    def debug_tee_stop(self) -> dict[str, Any]:
        self._tee_stop.set()
        th = self._tee_thread
        if th and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=1.5)
        self._tee_thread = None
        ser = self._tee_serial
        self._tee_serial = None
        device = self._tee_device
        self._tee_device = None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        return {"ok": True, "device": device}

    def _debug_tee_loop(
        self,
        log_path: Path,
        baud: int = 115200,
        wait: Optional[float] = None,
        dtr: bool = False,
    ) -> None:
        while not self._tee_stop.is_set():
            ser = self._tee_serial
            if ser is None:
                break
            try:
                n = ser.in_waiting if hasattr(ser, "in_waiting") else ser.inWaiting()
                data = ser.read(n or 1) if n else ser.read(1)
                if not data:
                    continue
                try:
                    with open(log_path, "ab") as f:
                        f.write(data)
                except Exception:
                    pass
                _notify(
                    "debug_tee_data",
                    {
                        "device": self._tee_device,
                        "data_b64": base64.b64encode(data).decode("ascii"),
                    },
                )
            except Exception as e:
                err: BaseException = e
                if wait and not self._tee_stop.is_set() and self._tee_device:
                    # The port went away (a native-USB board resetting): wait
                    # for it to come back and carry on reading.
                    try:
                        ser.close()
                    except Exception:
                        pass
                    _notify("debug_tee_lost", {"device": self._tee_device, "message": str(e)})
                    try:
                        ser = self._open_tee_serial(self._tee_device, baud, wait, dtr)
                        if self._tee_stop.is_set():
                            ser.close()
                            break
                        self._tee_serial = ser
                        continue
                    except Exception as e2:
                        err = e2
                    if self._tee_stop.is_set():
                        break
                _notify("debug_tee_error", {"message": str(err), "device": self._tee_device})
                break


SESSION = Session()

METHODS = {
    "ping": lambda _p: {
        "pong": True,
        "pid": os.getpid(),
        "platform": sys.platform,
        "session_id": resolve_session_id(),
    },
    "list_ports": lambda _p: SESSION.list_ports(),
    "connect": lambda p: SESSION.connect(
        p["device"],
        int(p.get("baud", 115200)),
        password=p.get("password"),
        known=bool(p.get("known", False)),
    ),
    "disconnect": lambda _p: (SESSION.disconnect() or {"ok": True}),
    "resume": lambda p: SESSION.resume(p.get("baud")),
    "fs_listdir": lambda p: SESSION.fs_listdir(p.get("path", "/")),
    "fs_stat": lambda p: SESSION.fs_stat(p["path"]),
    "fs_read": lambda p: SESSION.fs_read(p["path"]),
    "fs_write": lambda p: SESSION.fs_write(
        p["path"], p["data_b64"], bool(p.get("mpy", False)), bool(p.get("verify", False))
    ),
    "fs_mkdir": lambda p: SESSION.fs_mkdir(p["path"]),
    "fs_rm": lambda p: SESSION.fs_rm(p["path"]),
    "fs_rmdir": lambda p: SESSION.fs_rmdir(p["path"]),
    "fs_rm_rf": lambda p: SESSION.fs_rm_rf(p["path"]),
    "fs_touch": lambda p: SESSION.fs_touch(p["path"]),
    "fs_rename": lambda p: SESSION.fs_rename(p["src"], p["dest"]),
    "fs_hash": lambda p: SESSION.fs_hash(p["path"], p.get("algo", "sha256")),
    "fs_tree": lambda p: SESSION.fs_tree(p.get("path", "/")),
    "fs_cp": lambda p: SESSION.fs_cp(
        p["src"], p["dest"], bool(p.get("verify", False)), bool(p.get("mpy", False))
    ),
    "exec": lambda p: SESSION.exec(p["code"], bool(p.get("follow", True))),
    "eval": lambda p: SESSION.eval(p["expr"]),
    "run_script": lambda p: SESSION.run_script(
        p["source"], bool(p.get("follow", False))
    ),
    "run_path": lambda p: SESSION.run_path(p["path"], bool(p.get("follow", False))),
    "interrupt": lambda _p: SESSION.interrupt(),
    "soft_reset": lambda _p: SESSION.soft_reset(),
    "soft_reboot": lambda _p: SESSION.soft_reboot(),
    "hard_reset": lambda _p: SESSION.hard_reset(),
    "bootloader": lambda _p: SESSION.bootloader(),
    "debug_tee_start": lambda p: SESSION.debug_tee_start(
        p["device"],
        int(p.get("baud", 115200)),
        p.get("log_path"),
        p.get("wait"),
        bool(p.get("dtr", False)),
    ),
    "debug_tee_stop": lambda _p: SESSION.debug_tee_stop(),
    "rtc_get": lambda _p: SESSION.rtc_get(),
    "rtc_set": lambda _p: SESSION.rtc_set(),
    "mip_install": lambda p: SESSION.mip_install(
        list(p.get("packages") or []),
        p.get("target"),
        bool(p.get("mpy", True)),
        p.get("index"),
    ),
    "circup_install": lambda p: SESSION.circup_install(
        list(p.get("packages") or []),
        py=bool(p.get("py", False)),
        target=str(p.get("target") or "/lib"),
        host=p.get("host"),
        password=p.get("password"),
        prefer_web=bool(p.get("prefer_web", True)),
    ),
    "df": lambda _p: SESSION.df(),
    "mount": lambda p: SESSION.mount(p["path"], bool(p.get("unsafe_links", False))),
    "umount": lambda _p: SESSION.umount(),
    "romfs_query": lambda _p: SESSION.romfs_query(),
    "romfs_build": lambda p: SESSION.romfs_build(
        p["path"], p.get("output"), bool(p.get("mpy", True))
    ),
    "romfs_deploy": lambda p: SESSION.romfs_deploy(
        p["path"], int(p.get("partition", 0)), bool(p.get("mpy", True))
    ),
    "edit_pull": lambda p: SESSION.edit_pull(p["path"]),
    "edit_push": lambda p: SESSION.edit_push(p["path"], p["data_b64"]),
    "transfer_begin": lambda _p: SESSION.transfer_begin(),
    "transfer_end": lambda _p: SESSION.transfer_end(),
    "wifi_status": lambda _p: SESSION.wifi_status(),
    "wifi_access_plan": lambda p: SESSION.wifi_access_plan(p["action"], p.get("password")),
    "wifi_access_apply": lambda p: SESSION.wifi_access_apply(
        p["action"], p.get("password"), p.get("expect_sha256"), bool(p.get("now", False))
    ),
    "mdns_resolve": lambda p: SESSION.mdns_resolve(p["name"], p.get("timeout", 1.5)),
    "ble_scan": lambda p: {"boards": ble.scan(float(p.get("timeout", 5.0)))},
    "repl_start": lambda _p: SESSION.repl_start(),
    "repl_stop": lambda _p: SESSION.repl_stop(),
    "repl_write": lambda p: SESSION.repl_write(p["data_b64"]),
}


def main() -> None:
    import atexit

    session_id = resolve_session_id()
    os.environ["MPFTP_SESSION_ID"] = session_id
    killed = cleanup_stale_sidecars(session_id)
    claim_sidecar_pid(session_id)
    atexit.register(lambda: release_sidecar_pid(session_id))
    ready: dict[str, Any] = {
        "version": 1,
        "pid": os.getpid(),
        "session_id": session_id,
    }
    if killed:
        ready["killed_stale"] = killed
    _notify("ready", ready)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req_id = None
        try:
            msg = json.loads(line)
            req_id = msg.get("id")
            method = msg.get("method")
            params = msg.get("params") or {}
            if method not in METHODS:
                _error(req_id, f"unknown method: {method}")
                continue
            result = METHODS[method](params)
            _result(req_id, result)
        except Exception as e:
            _error_from_exception(req_id, e)
    try:
        SESSION.disconnect()
    except Exception:
        pass
    release_sidecar_pid()


if __name__ == "__main__":
    main()
