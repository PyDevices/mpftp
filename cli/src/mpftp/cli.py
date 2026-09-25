#!/usr/bin/env python3
"""
mpftp CLI — agent-friendly front-end to the mpftp sidecar / extension RPC.

Prefer the Cursor extension's Unix socket (~/.mpftp/rpc.sock) so CLI and UI
share one serial session. If the socket is missing, spawn mpftp.sidecar directly
(standalone; requires --device for board ops).

Examples:
  mpftp status
  mpftp ports
  mpftp connect COM4
  mpftp ls /
  mpftp put ./main.py /main.py
  mpftp get /main.py ./main.py
  mpftp eval '1+1'
  mpftp exec 'print(42)'
  mpftp interrupt
  mpftp soft-reset     # MP: skip main.py
  mpftp soft-reboot    # Ctrl-D; runs main.py / code.py
  mpftp run script.py  # default --no-follow (UI-safe)
  mpftp debug-tee COM50
  mpftp monitor COM4 --seconds 60 --log-path /tmp/con.log  # capture console (panic/stderr)
  mpftp hard-reset -d COM4 --monitor 15            # reset, then capture the boot
  mpftp watch          # tail activity log
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import __version__, ble, boards, config, webrepl, wifiboard


def _linux_home() -> Path:
    """Prefer the WSL/Linux home even if this script is run under Windows Python."""
    # If we're Windows Python launched from WSL, USERPROFILE is Windows; agents use Linux paths.
    wsl = os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP")
    linux_home = os.environ.get("HOME")
    if linux_home and (wsl or sys.platform.startswith("linux")):
        return Path(linux_home)
    # When python.exe runs with HOME unset to Linux, try /home/<user>
    if sys.platform == "win32":
        for cand in (
            os.environ.get("HOME"),
            "/home/" + os.environ.get("USER", ""),
            "/home/" + os.environ.get("USERNAME", "").lower(),
        ):
            if cand and cand.startswith("/home/") and Path(cand).is_dir():
                return Path(cand)
    return Path.home()


HOME_MPFTP = _linux_home() / ".mpftp"
# Also check Windows-side mirror when needed
WIN_MPFTP = Path.home() / ".mpftp"
ACTIVITY_LOG = HOME_MPFTP / "activity.log"
REPL_LOG = HOME_MPFTP / "repl.log"


def _parse_rpc_addr(text: str) -> Optional[tuple[str, int]]:
    text = (text or "").strip()
    if not text:
        return None
    # "127.0.0.1:7429" or legacy socket path
    if ":" in text and not text.startswith("/"):
        host, _, port_s = text.rpartition(":")
        try:
            return host.strip() or "127.0.0.1", int(port_s)
        except ValueError:
            return None
    return None


def _read_workspace_rpc_registry(path: Path) -> dict[str, dict[str, Any]]:
    """Parse one registry file.

    Values are the current ``{"addr", "editor", "pid", "updatedAt"}`` object
    shape (mpftp#21 — enough to tell two editors sharing a workspace root
    apart) or a legacy bare ``"host:port"`` string from an older extension
    build, normalized to ``{"addr": ...}`` either way.
    """
    try:
        if not path.is_file():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for k, v in raw.items():
            if not isinstance(k, str):
                continue
            if isinstance(v, str) and v.strip():
                out[k] = {"addr": v.strip()}
            elif isinstance(v, dict) and isinstance(v.get("addr"), str) and v["addr"].strip():
                entry: dict[str, Any] = {"addr": v["addr"].strip()}
                for field, want in (("editor", str), ("pid", int), ("updatedAt", str)):
                    if isinstance(v.get(field), want):
                        entry[field] = v[field]
                out[k] = entry
        return out
    except Exception:
        return {}


def _workspace_rpc_from_registry(start: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """Match cwd/parents against ``~/.mpftp/workspace-rpc.json`` (no repo litter).

    Returns the matched entry (at least ``addr``, plus ``editor``/``pid``/
    ``updatedAt`` when the extension that wrote it reported them), or None.
    """
    registries = [
        HOME_MPFTP / "workspace-rpc.json",
        WIN_MPFTP / "workspace-rpc.json",
    ]
    merged: dict[str, dict[str, Any]] = {}
    for reg_path in registries:
        merged.update(_read_workspace_rpc_registry(reg_path))
    if not merged:
        return None
    # Normalize keys once for prefix matching.
    norm: dict[str, dict[str, Any]] = {}
    for k, v in merged.items():
        try:
            norm[str(Path(k).resolve())] = v
        except Exception:
            norm[k] = v
    cur = (start or Path.cwd()).resolve()
    seen: set[Path] = set()
    for _ in range(48):
        if cur in seen:
            break
        seen.add(cur)
        entry = norm.get(str(cur))
        if entry:
            return entry
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


# Subprocesses run the installed package, not a sibling script file, so a
# private sidecar works the same whether mpftp was pip-installed or is being
# run from a checkout via PYTHONPATH.
SIDECAR = ["-m", "mpftp.sidecar"]
FIRMWARE_ENGINE = ["-m", "mpftp.firmware"]


def _die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def find_rpc_entry() -> Optional[dict[str, Any]]:
    """Full registry entry for the live extension RPC, if any.

    Same preference order as :func:`find_rpc_addr`, but keeps whatever
    diagnostic fields (``editor``, ``pid``, ``updatedAt``) came with it —
    `find_rpc_addr` throws those away, which is fine for connecting but not
    for telling a caller *whose* session they resolved to (mpftp#21).
    """
    env = (os.environ.get("MPFTP_RPC") or "").strip()
    if env:
        parsed = _parse_rpc_addr(env)
        if parsed:
            return {"addr": f"{parsed[0]}:{parsed[1]}", "source": "MPFTP_RPC"}

    return _workspace_rpc_from_registry()


def find_rpc_addr() -> Optional[tuple[str, int]]:
    """Return (host, port) for the extension AgentRpcServer, if running.

    The listener binds an ephemeral port only while a board is connected in
    some window, so there is no fixed port to fall back to and no home-wide
    "last writer" file that could mean anything once more than one window can
    be connected at once. If neither of these name a live session, the caller
    falls back to spawning its own private sidecar.

    Preference order:

    1. ``MPFTP_RPC`` env (``127.0.0.1:PORT``)
    2. ``~/.mpftp/workspace-rpc.json`` match for cwd/parents (per-window, no repo litter)
    """
    entry = find_rpc_entry()
    if not entry:
        return None
    return _parse_rpc_addr(entry.get("addr", ""))


class RpcError(RuntimeError):
    """An ``{"type": "error"}`` reply from the sidecar or extension RPC.

    ``partial_output`` is what the board printed before a ``run --follow``
    timed out (mpftp#25); None for every other error.
    """

    def __init__(self, message: str, partial_output: Optional[str] = None) -> None:
        super().__init__(message)
        self.partial_output = partial_output


def _rpc_error(msg: dict, default: str) -> RpcError:
    return RpcError(msg.get("error") or default, msg.get("partialOutput"))


_TEE_NOTIFIES = ("debug_tee_data", "debug_tee_error", "debug_tee_open", "debug_tee_lost")


def _tee_reset_clock(
    method: str, deadline: Optional[float], duration: Optional[float]
) -> tuple[Optional[float], bool]:
    """A waiting capture's clock: (new deadline, stop now?).

    It runs for ``duration`` from the last time the port opened, stands still
    while the port is gone (the sidecar bounds that wait itself), and stops
    when the sidecar gives up on the port.
    """
    if method == "debug_tee_open" and duration is not None:
        return time.time() + duration, False
    if method == "debug_tee_lost":
        return None, False
    if method == "debug_tee_error":
        return deadline, True
    return deadline, False


class RpcClient:
    def call(self, method: str, params: Optional[dict] = None) -> Any:
        raise NotImplementedError

    def stream_repl(
        self, on_notify: Callable[[str, dict], None], duration: Optional[float] = None
    ) -> None:
        """Non-interrupting live tail of the board's own stdout (mpftp#10).

        Never enters raw REPL, so a running script keeps running — this is
        not a way to read an arbitrary board file, only what the board
        itself prints. With ``duration`` omitted, blocks until the connection
        ends or the caller raises (e.g. KeyboardInterrupt on Ctrl-C, which
        stops *watching*, not the board — no bytes are ever sent to it).
        With ``duration`` set, returns after that many seconds (a bounded
        capture — the MCP ``watch_repl`` tool needs a call that returns).
        """
        raise NotImplementedError

    def stream_debug_tee(
        self,
        device: str,
        baud: int,
        log_path: Optional[str],
        on_notify: Callable[[str, dict], None],
        duration: Optional[float] = None,
        wait: Optional[float] = None,
        dtr: bool = False,
    ) -> None:
        """Read-only console capture on a second COM, held open for a duration.

        With ``wait``, the sidecar waits up to that long for the port to
        (re)appear, and ``duration`` counts from the last time it opened
        (each ``debug_tee_open`` notify; ``hard-reset --monitor``, mpftp#60).

        Unlike :meth:`call` + ``debug_tee_start`` (which stops the moment the
        CLI returns and closes the private sidecar, so the tee died with it),
        this keeps the session alive so the sidecar's tee loop keeps
        writing ``log_path`` and emitting ``debug_tee_data`` the whole time.
        Never enters raw REPL and never toggles DTR/RTS, so a board autostarted
        from ``main.py`` keeps running and its panic backtrace / ``stderr`` is
        captured. Returns after ``duration`` seconds (or on KeyboardInterrupt).
        """
        raise NotImplementedError

    def close(self) -> None:
        pass


class TcpClient(RpcClient):
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._id = 0

    def call(self, method: str, params: Optional[dict] = None) -> Any:
        self._id += 1
        req = {"id": self._id, "method": method, "params": params or {}}
        with socket.create_connection((self.host, self.port), timeout=120) as s:
            s.sendall((json.dumps(req) + "\n").encode("utf-8"))
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if b"\n" in buf:
                    break
        line = buf.split(b"\n", 1)[0].decode("utf-8", "replace")
        if not line.strip():
            # A bare json.JSONDecodeError here ("Expecting value: line 1
            # column 1") reads as a parser bug; it's actually the extension's
            # RPC connection closing without a reply (dead session, wedged
            # port) — say that instead (mpftp#15).
            raise RuntimeError(
                f"no response from mpftp RPC session at {self.host}:{self.port} "
                "(connection closed before a reply arrived)"
            )
        msg = json.loads(line)
        if msg.get("type") == "error":
            raise _rpc_error(msg, "rpc error")
        return msg.get("result")

    def stream_repl(
        self, on_notify: Callable[[str, dict], None], duration: Optional[float] = None
    ) -> None:
        self._id += 1
        req = {"id": self._id, "method": "repl_stream", "params": {}}
        deadline = time.time() + duration if duration is not None else None
        with socket.create_connection((self.host, self.port), timeout=None) as s:
            s.sendall((json.dumps(req) + "\n").encode("utf-8"))
            buf = b""
            while True:
                if deadline is not None:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        return
                    s.settimeout(remaining)
                try:
                    chunk = s.recv(65536)
                except socket.timeout:
                    return
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", "replace").strip()
                    if not text:
                        continue
                    msg = json.loads(text)
                    if msg.get("type") == "error":
                        raise RuntimeError(msg.get("error") or "rpc error")
                    if msg.get("type") == "notify" and msg.get("method") in (
                        "repl_data",
                        "repl_error",
                    ):
                        on_notify(msg["method"], msg.get("params") or {})

    def stream_debug_tee(
        self,
        device: str,
        baud: int,
        log_path: Optional[str],
        on_notify: Callable[[str, dict], None],
        duration: Optional[float] = None,
        wait: Optional[float] = None,
        dtr: bool = False,
    ) -> None:
        self._id += 1
        start_id = self._id
        params: dict[str, Any] = {"device": device, "baud": baud, "log_path": log_path}
        if wait:
            params["wait"] = wait
        if dtr:
            params["dtr"] = True
        req = {"id": start_id, "method": "debug_tee_start", "params": params}
        # With wait, the clock starts at each debug_tee_open notify.
        deadline = (
            time.time() + duration if duration is not None and not wait else None
        )
        try:
            with socket.create_connection((self.host, self.port), timeout=None) as s:
                s.sendall((json.dumps(req) + "\n").encode("utf-8"))
                buf = b""
                while True:
                    if deadline is not None:
                        remaining = deadline - time.time()
                        if remaining <= 0:
                            return
                        s.settimeout(remaining)
                    else:
                        # The clock can stop (debug_tee_lost): so must the timeout.
                        s.settimeout(None)
                    try:
                        chunk = s.recv(65536)
                    except socket.timeout:
                        return
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        text = line.decode("utf-8", "replace").strip()
                        if not text:
                            continue
                        msg = json.loads(text)
                        if msg.get("type") == "error":
                            raise RuntimeError(msg.get("error") or "rpc error")
                        if msg.get("type") == "notify" and msg.get("method") in _TEE_NOTIFIES:
                            on_notify(msg["method"], msg.get("params") or {})
                            if wait:
                                deadline, done = _tee_reset_clock(
                                    msg["method"], deadline, duration
                                )
                                if done:
                                    return
        finally:
            # The tee lives in the shared session, not in this socket: closing
            # the stream leaves it reading the COM forever, so the next
            # connect finds the port busy and the log keeps growing. The
            # subprocess client stops it on its own pipe; here a fresh call()
            # is enough, and a dead session is not worth raising over.
            try:
                self.call("debug_tee_stop")
            except Exception:
                pass


def _is_windows_python(python: str) -> bool:
    p = python.lower()
    return p.endswith(".exe") or "/mnt/c/" in p or bool(re.match(r"^[a-z]:\\", p))


# Env vars a Windows child spawned from WSL silently does not receive unless
# named in WSLENV — MICROPYPATH is the reported case (mpftp#12): a Windows
# Windows python.exe spawned from WSL only sees env vars listed in WSLENV.
# /l = one path, /p = path list (PYTHONPATH). Without PYTHONPATH, a checkout
# CLI would spawn the pip-installed sidecar and ignore local edits.
_WSLENV_FORWARD = (
    ("MICROPYPATH", "l"),
    ("PYTHONPATH", "p"),
    # ble:// test switches (see ble.py); plain values.
    (ble.PLANT_ENV, ""),
    (ble.FILES_ENV, ""),
)


#: init's interop socket. Always live, where a shell's own $WSL_INTEROP dies
#: with the shell that opened it (mpftp#28).
_WSL_INTEROP_FALLBACK = "/run/WSL/2_interop"


def _live_wsl_interop() -> Optional[str]:
    """A usable ``WSL_INTEROP`` value, or None to leave the environment alone.

    A long-lived shell can hold a ``WSL_INTEROP`` path whose owner is gone. The
    socket file is still named in the environment and every ``.exe`` launch
    then times out on ``accept4`` with errno 110, which surfaces as a sidecar
    that dies before ``ready``. init's socket is always there, so when the
    inherited one has vanished, point at that instead.
    """
    current = os.environ.get("WSL_INTEROP")
    if not current or os.path.exists(current):
        return None
    if os.path.exists(_WSL_INTEROP_FALLBACK):
        return _WSL_INTEROP_FALLBACK
    return None


def _sidecar_died_message(stderr: str) -> str:
    """What to tell the user when the sidecar dies before ``ready``.

    The generic message, plus the port hint further down, sent a bench session
    hunting a serial fault for an hour when the board and the port were both
    fine: a stale ``WSL_INTEROP`` socket makes every Windows ``.exe`` launch
    time out on ``accept4`` (mpftp#28). If the sidecar's stderr says so, say so.
    """
    text = stderr or ""
    if "UtilAcceptVsock" in text or "accept4 failed" in text:
        return (
            "the Windows-python sidecar could not be launched over WSL interop "
            "(stale WSL_INTEROP socket) -- this is not the serial port, and not "
            "the board.\n"
            f"  Try: export WSL_INTEROP={_WSL_INTEROP_FALLBACK}   "
            "(init's socket, always live), then re-run.\n"
            "  Heavier alternative: wsl --shutdown.\n"
            f"  sidecar stderr: {text.strip()}"
        )
    return f"sidecar exited early: {text}"


def _wslenv_forwarded_env(python: str) -> Optional[dict]:
    """Env for spawning ``python``, with WSLENV augmented if it's a Windows
    binary launched from WSL. Returns None when nothing needs to change, so
    the caller can pass it straight to ``env=`` (None means "inherit")."""
    wsl = os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP")
    if not wsl or not _is_windows_python(python):
        return None
    interop = _live_wsl_interop()
    to_forward = [(v, flag) for v, flag in _WSLENV_FORWARD if os.environ.get(v) is not None]
    existing = [e.strip() for e in os.environ.get("WSLENV", "").split(":") if e.strip()]
    already = {e.split("/")[0] for e in existing}
    additions = [f"{v}/{flag}" if flag else v for v, flag in to_forward if v not in already]
    if not additions and interop is None:
        return None
    env = dict(os.environ)
    if interop is not None:
        env["WSL_INTEROP"] = interop
    if additions:
        env["WSLENV"] = ":".join(existing + additions)
    return env


def _wsl_path_for_windows_sidecar(path: str) -> str:
    """Translate a POSIX path to one a Windows-side sidecar can open.

    debug-tee's sidecar always runs under Windows python on WSL (same
    resolution as connect, for COM access), so a POSIX ``--log-path`` like
    ``/tmp/tee.log`` was silently interpreted by ``pathlib`` as a relative
    Windows path (``\\tmp\\tee.log``) — no file ever appeared where the
    caller expected it (mpftp#13).
    """
    wsl = os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP")
    if not wsl or not path.startswith("/"):
        return path
    try:
        win = subprocess.run(
            ["wslpath", "-w", path], capture_output=True, text=True, timeout=3
        )
        out = win.stdout.strip()
        if win.returncode == 0 and out:
            return out
    except Exception:
        pass
    distro = os.environ.get("WSL_DISTRO_NAME") or "Ubuntu"
    return f"\\\\wsl.localhost\\{distro}" + path.replace("/", "\\")


class SidecarClient(RpcClient):
    """One-shot sidecar process; connect yourself before board ops."""

    def __init__(self, python: str) -> None:
        self.python = python
        self.proc = subprocess.Popen(
            [python, *SIDECAR],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=_wslenv_forwarded_env(python),
        )
        self._id = 0
        self._reader_active = False
        assert self.proc.stdout
        # wait for ready
        deadline = time.time() + 20
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                err = self.proc.stderr.read() if self.proc.stderr else ""
                _die(_sidecar_died_message(err))
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "notify" and msg.get("method") == "ready":
                break
        else:
            _die("sidecar ready timeout")

    def call(self, method: str, params: Optional[dict] = None) -> Any:
        assert self.proc.stdin and self.proc.stdout
        self._id += 1
        self.proc.stdin.write(json.dumps({"id": self._id, "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                err = self.proc.stderr.read() if self.proc.stderr else ""
                raise RuntimeError(f"sidecar closed: {err}")
            msg = json.loads(line)
            if msg.get("type") == "notify":
                continue
            if msg.get("id") != self._id:
                continue
            if msg.get("type") == "error":
                raise _rpc_error(msg, "sidecar error")
            return msg.get("result")

    def stream_repl(
        self, on_notify: Callable[[str, dict], None], duration: Optional[float] = None
    ) -> None:
        assert self.proc.stdin and self.proc.stdout
        self._id += 1
        self.proc.stdin.write(
            json.dumps({"id": self._id, "method": "repl_start", "params": {}}) + "\n"
        )
        self.proc.stdin.flush()

        # A plain readline() loop can't be bounded by a wall-clock duration,
        # so read on a daemon thread and poll a queue with a timeout instead.
        # The thread outlives an expired duration (it dies with the sidecar
        # process on close()), which is fine for a one-shot bounded capture.
        lines: "queue.Queue[Optional[str]]" = queue.Queue()

        def reader() -> None:
            while True:
                line = self.proc.stdout.readline()
                lines.put(line or None)
                if not line:
                    return

        threading.Thread(target=reader, daemon=True).start()
        # The daemon reader owns stdout from here on; close() must not issue a
        # graceful disconnect RPC (it would deadlock fighting for the pipe).
        self._reader_active = True
        deadline = time.time() + duration if duration is not None else None
        while True:
            remaining = (deadline - time.time()) if deadline is not None else None
            try:
                line = lines.get(timeout=max(0.0, remaining) if remaining is not None else None)
            except queue.Empty:
                return
            if not line:
                err = self.proc.stderr.read() if self.proc.stderr else ""
                raise RuntimeError(f"sidecar closed: {err}")
            msg = json.loads(line)
            if msg.get("type") == "notify" and msg.get("method") in (
                "repl_data",
                "repl_error",
            ):
                on_notify(msg["method"], msg.get("params") or {})
                continue
            if msg.get("id") == self._id and msg.get("type") == "error":
                raise RuntimeError(msg.get("error") or "sidecar error")

    def stream_debug_tee(
        self,
        device: str,
        baud: int,
        log_path: Optional[str],
        on_notify: Callable[[str, dict], None],
        duration: Optional[float] = None,
        wait: Optional[float] = None,
        dtr: bool = False,
    ) -> None:
        assert self.proc.stdin and self.proc.stdout
        self._id += 1
        start_id = self._id
        params: dict[str, Any] = {"device": device, "baud": baud, "log_path": log_path}
        if wait:
            params["wait"] = wait
        if dtr:
            params["dtr"] = True
        self.proc.stdin.write(
            json.dumps({"id": start_id, "method": "debug_tee_start", "params": params})
            + "\n"
        )
        self.proc.stdin.flush()

        # Read on a daemon thread so a wall-clock duration can bound the
        # capture (a plain readline() can't). The sidecar's tee loop writes
        # log_path itself; draining here keeps its stdout pipe from filling
        # and stalling that loop.
        lines: "queue.Queue[Optional[str]]" = queue.Queue()

        def reader() -> None:
            while True:
                line = self.proc.stdout.readline()
                lines.put(line or None)
                if not line:
                    return

        threading.Thread(target=reader, daemon=True).start()
        # The daemon reader owns stdout for the rest of this process, so a
        # later close()/self.call() would deadlock fighting it for the pipe.
        # Mark the session streaming so close() just terminates the proc.
        self._reader_active = True
        # With wait, the clock starts at each debug_tee_open notify.
        deadline = (
            time.time() + duration if duration is not None and not wait else None
        )
        try:
            while True:
                remaining = (deadline - time.time()) if deadline is not None else None
                if remaining is not None and remaining <= 0:
                    return
                try:
                    line = lines.get(
                        timeout=max(0.0, remaining) if remaining is not None else None
                    )
                except queue.Empty:
                    return
                if not line:
                    err = self.proc.stderr.read() if self.proc.stderr else ""
                    raise RuntimeError(f"sidecar closed: {err}")
                msg = json.loads(line)
                if msg.get("type") == "notify" and msg.get("method") in _TEE_NOTIFIES:
                    on_notify(msg["method"], msg.get("params") or {})
                    if wait:
                        deadline, done = _tee_reset_clock(msg["method"], deadline, duration)
                        if done:
                            return
                    continue
                if msg.get("id") == start_id and msg.get("type") == "error":
                    raise RuntimeError(msg.get("error") or "sidecar error")
        finally:
            # The reader thread still owns stdout, so don't use self.call()
            # here (it would race for the pipe). Fire-and-forget the stop so
            # the sidecar releases the COM port; the reader drains the reply.
            self._id += 1
            try:
                self.proc.stdin.write(
                    json.dumps({"id": self._id, "method": "debug_tee_stop", "params": {}})
                    + "\n"
                )
                self.proc.stdin.flush()
                time.sleep(0.3)
            except Exception:
                pass

    def close(self) -> None:
        # A streaming capture (stream_repl/stream_debug_tee) left a daemon
        # reader owning stdout; a graceful disconnect RPC would deadlock
        # fighting it for the pipe, so skip straight to terminating the proc.
        if not getattr(self, "_reader_active", False):
            try:
                self.call("disconnect")
            except Exception:
                pass
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except Exception:
                self.proc.kill()


def resolve_python() -> str:
    env = os.environ.get("MPFTP_PYTHON")
    if env:
        return env
    configured = config.resolve("pythonPath")
    if configured:
        return configured
    # Prefer Windows python on WSL for COM ports
    for cand in (
        str(Path.home() / "bin" / "python.exe"),
        "python.exe",
        str(Path(__file__).resolve().parents[3] / ".venv" / "bin" / "python"),
        "python3",
        "python",
    ):
        try:
            r = subprocess.run(
                [cand, "-c", "import mpremote, serial; print('ok')"],
                capture_output=True,
                # WSL interop hands python.exe our stdin, and it would eat a
                # confirmation meant for us (mpftp wifi enable).
                stdin=subprocess.DEVNULL,
                timeout=15,
            )
            if r.returncode == 0:
                return cand
        except Exception:
            continue
    return "python3"


def get_client(prefer_rpc: bool = True) -> tuple[RpcClient, str]:
    if prefer_rpc:
        addr = find_rpc_addr()
        if addr:
            host, port = addr
            client = TcpClient(host, port)
            try:
                # A surviving extension RPC listener can outlive its Python
                # sidecar after an editor/WSL restart.  Probe the sidecar, not
                # merely the TCP socket, before committing this command to RPC.
                client.call("ping")
                return client, f"tcp:{host}:{port}"
            except Exception:
                # Standalone mode is the documented recovery path when the
                # extension session is unavailable.  Board operations still
                # require --device so this does not guess a serial target.
                pass
    return SidecarClient(resolve_python()), "sidecar"


def out(obj: Any) -> None:
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, indent=2, ensure_ascii=False))
    else:
        print(obj)


def _hint_for(msg: str) -> Optional[str]:
    """Best-effort actionable next step for a bare exception message.

    Errors that already carry a checklist (e.g. sidecar's "could not take
    control...  Your options: ...") are left alone — anything more would just
    repeat them.
    """
    low = msg.lower()
    if "no response from mpftp rpc session" in low:
        return (
            "the extension's RPC session did not reply — the sidecar or "
            "serial port may be dead or busy. Try `mpftp status`, then "
            "`connect`/`resume`."
        )
    if "expecting value" in low or "extra data" in low:
        return (
            "no valid response from the RPC session — the sidecar or serial "
            "port may be dead or busy. Try `mpftp status`, then `connect`/`resume`."
        )
    if "connection refused" in low or "connection reset" in low or "broken pipe" in low:
        return (
            "could not reach the extension RPC session. Try `mpftp status`, "
            "or run again without one active to spawn a private sidecar."
        )
    if "errno 2" in low or "no such file" in low:
        return "path not found — check it exists (on the board or host) and is spelled correctly."
    return None


def _emit_error_envelope(exc: BaseException) -> None:
    """Structured {"ok": false, "error", "hint"} on stdout, matching the JSON
    shape a successful command's `out()` would have produced (mpftp#15).
    A timed-out ``run --follow`` adds "partialOutput": what the board printed
    before it stopped, on the stream the output would have gone to (mpftp#25)."""
    msg = str(exc)
    envelope: dict[str, Any] = {"ok": False, "error": msg}
    hint = _hint_for(msg)
    if hint:
        envelope["hint"] = hint
    partial = getattr(exc, "partial_output", None)
    if partial is not None:
        envelope["partialOutput"] = partial
    print(json.dumps(envelope, indent=2, ensure_ascii=False))


def cmd_status(_: argparse.Namespace) -> None:
    entry = find_rpc_entry()
    addr = _parse_rpc_addr(entry.get("addr", "")) if entry else None
    info: dict[str, Any] = {
        "rpc": f"{addr[0]}:{addr[1]}" if addr else None,
        "rpc_preference": "MPFTP_RPC > ~/.mpftp/workspace-rpc.json (cwd match) > spawn a private sidecar",
        "workspace_rpc_registry": str(HOME_MPFTP / "workspace-rpc.json"),
        "activity_log": str(ACTIVITY_LOG),
        "repl_log": str(REPL_LOG),
        "extension_running": bool(addr),
    }
    # editor/pid/updatedAt, when the extension that registered this address
    # reported them -- lets two editors sharing a workspace root be told
    # apart instead of "a session exists" with no way to tell whose (mpftp#21).
    if entry:
        for field in ("editor", "pid", "updatedAt"):
            if field in entry:
                info[field] = entry[field]
    if addr:
        try:
            client: RpcClient = TcpClient(*addr)
            info["session"] = client.call("agent_status")
        except Exception as e:
            info["session_error"] = str(e)
    out(info)


def cmd_ports(_: argparse.Namespace) -> None:
    client, _ = get_client()
    try:
        ports = client.call("list_ports")
        out(ports)
    finally:
        client.close()


def connect_params(device: str, baud: int) -> dict[str, Any]:
    """``connect`` RPC params. A ws:// device carries the WebREPL password,
    resolved here so it comes from this user's environment or config file
    (a Windows sidecar spawned from WSL sees neither)."""
    params: dict[str, Any] = {"device": device, "baud": baud}
    if ble.is_ble_device(device):
        # ~/.mpftp/webrepl-passwords.json under ble:<name>, else
        # MPFTP_BLE_PASSWORD / blePassword, else the WebREPL one.
        password = boards.get_password(device)
        if password:
            params["password"] = password
        return params
    if webrepl.is_network_device(device):
        # This board's own password (~/.mpftp/webrepl-passwords.json), else
        # MPFTP_WEBREPL_PASSWORD / webreplPassword.
        password = boards.get_password(device)
        if password:
            params["password"] = password
        if boards.uid_for_device(device):
            params["known"] = True
    return params


def connect_device(client: RpcClient, device: str, baud: int) -> Any:
    """``connect``, then remember the board if its Wi-Fi is up."""
    params = connect_params(device, baud)
    res = client.call("connect", params)
    boards.record_connect(device, res, params.get("password"))
    return res


def cmd_connect(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        res = connect_device(client, ns.device, ns.baud)
        print(f"connected via {mode}: {res}", file=sys.stderr)
        out(res)
    finally:
        if mode.startswith("sidecar"):
            # keep process? one-shot connect is useless in sidecar mode without linger
            client.close()


def cmd_disconnect(_: argparse.Namespace) -> None:
    client, _ = get_client()
    try:
        out(client.call("disconnect"))
    finally:
        client.close()


def cmd_resume(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        params: dict[str, Any] = {}
        if ns.baud:
            params["baud"] = ns.baud
        out(client.call("resume", params))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def ensure_device(client: RpcClient, device: Optional[str], baud: int) -> None:
    if not device:
        return
    connect_device(client, device, baud)


def cmd_ls(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        entries = client.call("fs_listdir", {"path": ns.path})
        if ns.json:
            out(entries)
            return
        for e in entries or []:
            kind = "d" if e.get("isDir") else "-"
            print(f"{kind} {e.get('size', 0):8}  {e.get('name')}")
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_tree(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("fs_tree", {"path": ns.path}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_put(ns: argparse.Namespace) -> None:
    local = Path(ns.local)
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        dest = ns.remote
        mpy = bool(getattr(ns, "mpy", False))
        verify = bool(getattr(ns, "verify", True))
        if getattr(ns, "recursive", False) or local.is_dir():
            out(
                client.call(
                    "fs_cp",
                    {
                        "src": str(local.resolve()),
                        "dest": ":" + dest if not dest.startswith(":") else dest,
                        "verify": verify,
                        "mpy": mpy,
                    },
                )
            )
            return
        data = local.read_bytes()
        if mpy:
            # The board may compile to a different remote path (.py -> .mpy); the
            # source bytes on the CLI side aren't what ends up on the board, so
            # verification has to happen sidecar-side against the compiled output.
            out(
                client.call(
                    "fs_write",
                    {
                        "path": dest,
                        "data_b64": base64.b64encode(data).decode("ascii"),
                        "mpy": True,
                        "verify": verify,
                    },
                )
            )
            return
        res = client.call(
            "fs_write",
            {"path": dest, "data_b64": base64.b64encode(data).decode("ascii")},
        )
        if verify:
            import hashlib

            expect = hashlib.sha256(data).hexdigest()
            got = client.call("fs_hash", {"path": dest, "algo": "sha256"})["hash"]
            if got != expect:
                raise SystemExit(f"hash mismatch: expected {expect}, got {got}")
            res = {**res, "verified": got}
        out(res)
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_get(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        remote = ns.remote
        if getattr(ns, "recursive", False):
            out(
                client.call(
                    "fs_cp",
                    {
                        "src": ":" + remote if not remote.startswith(":") else remote,
                        "dest": str(Path(ns.local).resolve()),
                        "verify": bool(getattr(ns, "verify", True)),
                    },
                )
            )
            return
        res = client.call("fs_read", {"path": remote})
        raw = base64.b64decode(res["data_b64"])
        Path(ns.local).write_bytes(raw)
        if getattr(ns, "verify", True):
            import hashlib

            expect = client.call("fs_hash", {"path": remote, "algo": "sha256"})["hash"]
            got = hashlib.sha256(raw).hexdigest()
            if got != expect:
                raise SystemExit(f"hash mismatch: expected {expect}, got {got}")
        print(f"wrote {len(raw)} bytes → {ns.local}", file=sys.stderr)
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_cp(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(
            client.call(
                "fs_cp",
                {
                    "src": ns.src,
                    "dest": ns.dest,
                    "verify": bool(ns.verify),
                    "mpy": bool(getattr(ns, "mpy", False)),
                },
            )
        )
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_hash(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("fs_hash", {"path": ns.path, "algo": ns.algo}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_edit(ns: argparse.Namespace) -> None:
    import os
    import tempfile

    editor = os.environ.get("EDITOR")
    if not editor:
        raise SystemExit("edit: $EDITOR not set")
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        path = ns.path
        client.call("fs_touch", {"path": path})
        res = client.call("edit_pull", {"path": path})
        raw = base64.b64decode(res["data_b64"])
        fd, tmp = tempfile.mkstemp(suffix="-" + Path(path).name)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            rc = os.system(f'{editor} "{tmp}"')
            if rc != 0:
                raise SystemExit(f"editor exited {rc}")
            data = Path(tmp).read_bytes()
            out(
                client.call(
                    "edit_push",
                    {"path": path, "data_b64": base64.b64encode(data).decode("ascii")},
                )
            )
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_romfs(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        if ns.romfs_cmd == "build":
            # build is host-only; still needs a client for method dispatch
            out(
                client.call(
                    "romfs_build",
                    {"path": ns.path, "output": ns.output, "mpy": not ns.no_mpy},
                )
            )
            return
        ensure_device(client, ns.device, ns.baud)
        if ns.romfs_cmd == "query":
            out(client.call("romfs_query"))
        elif ns.romfs_cmd == "deploy":
            out(
                client.call(
                    "romfs_deploy",
                    {
                        "path": ns.path,
                        "partition": ns.partition,
                        "mpy": not ns.no_mpy,
                    },
                )
            )
        else:
            raise SystemExit(f"unknown romfs command: {ns.romfs_cmd}")
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_mkdir(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("fs_mkdir", {"path": ns.path}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_rm(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        method = "fs_rm_rf" if ns.recursive else "fs_rm"
        out(client.call(method, {"path": ns.path}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_touch(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("fs_touch", {"path": ns.path}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_rename(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("fs_rename", {"src": ns.src, "dest": ns.dest}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_eval(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("eval", {"expr": ns.expr}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_exec(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        follow = not bool(getattr(ns, "no_follow", False))
        out(client.call("exec", {"code": ns.code, "follow": follow}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def _wait_and_reconnect(
    client: RpcClient, device: str, baud: int, *, attempts: int = 20, delay: float = 1.0
) -> None:
    """Poll for ``device`` to come back after a reset, then reconnect.

    Stale module state from a previous run (armed timers, a re-imported
    board_config) makes an otherwise-fine board look broken a couple of
    iterations in — this is what --reboot-first is for (mpftp#11).
    """
    last_err: Optional[Exception] = None
    for _ in range(max(1, attempts)):
        time.sleep(delay)
        try:
            if not (webrepl.is_network_device(device) or ble.is_ble_device(device)):
                ports = client.call("list_ports")
                if not any((p or {}).get("device") == device for p in ports or []):
                    continue
            connect_device(client, device, baud)
            return
        except Exception as e:
            last_err = e
    raise RuntimeError(f"could not reconnect to {device} after reboot: {last_err}")


def run_probe(
    client: "RpcClient",
    *,
    device: Optional[str],
    baud: int,
    file: str,
    reboot_first: bool = False,
    capture: Optional[str] = None,
    wait: float = 0.0,
) -> dict[str, Any]:
    """run -> wait -> capture in one shot: the agent loop for anything that
    outlives a raw-REPL session (mpftp#11). Shared by the CLI and the MCP
    ``probe`` tool."""
    ensure_device(client, device, baud)

    if reboot_first:
        if not device:
            raise RuntimeError("probe --reboot-first requires --device to reconnect to")
        client.call("hard_reset")
        _wait_and_reconnect(client, device, baud)

    source = Path(file).read_text(encoding="utf-8")
    client.call("run_script", {"source": source, "follow": False})

    if wait:
        time.sleep(wait)

    result: dict[str, Any] = {"ok": True, "script": file}
    if capture:
        try:
            res = client.call("fs_read", {"path": capture})
            raw = base64.b64decode(res["data_b64"])
            result["capture"] = {
                "path": capture,
                "size": len(raw),
                "text": raw.decode("utf-8", "replace"),
            }
        except Exception as e:
            result["ok"] = False
            result["capture_error"] = str(e)
    return result


def cmd_probe(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        out(
            run_probe(
                client,
                device=ns.device,
                baud=ns.baud,
                file=ns.file,
                reboot_first=ns.reboot_first,
                capture=ns.capture,
                wait=ns.wait,
            )
        )
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_run(ns: argparse.Namespace) -> None:
    source = Path(ns.file).read_text(encoding="utf-8")
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        # Default no-follow so UI apps do not wedge the COM handle (mpftp#3).
        follow = bool(getattr(ns, "follow", False))
        out(client.call("run_script", {"source": source, "follow": follow}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_interrupt(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("interrupt"))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_soft_reset(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("soft_reset"))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_soft_reboot(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("soft_reboot"))
    finally:
        if mode.startswith("sidecar"):
            client.close()


# How long hard-reset --monitor waits for the port to come back. A native-USB
# board drops off the bus and re-enumerates in a few seconds; a USB-UART
# bridge never goes away.
RESET_MONITOR_WAIT = 20.0


def cmd_hard_reset(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        result = client.call("hard_reset")
        if ns.monitor is None:
            out(result)
            return
        device = (result or {}).get("device") or ns.device
        if not device:
            raise RuntimeError("hard-reset --monitor needs -d/--device")
        print(json.dumps(result), file=sys.stderr)
        _monitor_stream(
            client,
            device,
            ns.baud,
            ns.log_path,
            float(ns.monitor),
            wait=RESET_MONITOR_WAIT,
            dtr=bool((result or {}).get("console_dtr")),
        )
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_debug_tee(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        if ns.stop:
            out(client.call("debug_tee_stop"))
            return
        if not ns.device_tee:
            raise SystemExit("debug-tee requires a device (e.g. COM50) or --stop")
        log_path = _wsl_path_for_windows_sidecar(ns.log_path) if ns.log_path else ns.log_path
        out(
            client.call(
                "debug_tee_start",
                {
                    "device": ns.device_tee,
                    "baud": ns.baud,
                    "log_path": log_path,
                },
            )
        )
    finally:
        client.close()


def cmd_monitor(ns: argparse.Namespace) -> None:
    """Read-only console capture on a COM port, held open for a duration.

    This is the capture that ``debug-tee`` could not do from the CLI: the
    one-shot ``debug-tee`` returned immediately and the private sidecar (and
    its tee) died with the command, so the log stayed empty. ``monitor`` keeps
    the session alive for ``--seconds`` (or until Ctrl-C), streaming bytes to
    stdout and appending them to ``--log-path``. It never enters raw REPL and
    never toggles DTR/RTS, so a board autostarted from ``main.py`` keeps
    running and its ``stderr`` / panic backtrace is captured.

    Point it at the ESP console UART (the same COM as the REPL when the board
    is *not* under mpftp control, e.g. running main.py) or the native USB CDC
    debug port — whichever carries the firmware's console output.
    """
    client, mode = get_client()
    try:
        _monitor_stream(
            client,
            ns.device_mon,
            ns.baud,
            ns.log_path,
            float(ns.seconds) if ns.seconds else None,
        )
    finally:
        client.close()


def _monitor_stream(
    client: RpcClient,
    device: str,
    baud: int,
    log_path: Optional[str],
    duration: Optional[float],
    *,
    wait: Optional[float] = None,
    dtr: bool = False,
) -> None:
    """Stream ``device``'s console read-only to stdout (and ``log_path``).

    Shared by ``monitor`` and ``hard-reset --monitor``; ``wait`` is how long
    the sidecar waits for a port that is re-enumerating, and ``duration``
    then counts from when it opened.
    """
    log_path = _wsl_path_for_windows_sidecar(log_path) if log_path else log_path
    print(
        "monitoring %s @ %d baud (read-only, %s%s) ..."
        % (
            device,
            baud,
            ("%gs" % duration) if duration else "Ctrl-C to stop",
            (", waiting up to %gs for the port" % wait) if wait else "",
        ),
        file=sys.stderr,
    )

    def on_notify(method: str, params: dict) -> None:
        if method == "debug_tee_data":
            b64 = params.get("data_b64")
            if b64:
                sys.stdout.buffer.write(base64.b64decode(b64))
                sys.stdout.buffer.flush()
        elif method == "debug_tee_error":
            print(f"[debug_tee_error] {params.get('message')}", file=sys.stderr)
        elif method == "debug_tee_open":
            print(
                "[%s open after %ss]" % (params.get("device"), params.get("after_s")),
                file=sys.stderr,
            )
        elif method == "debug_tee_lost":
            print(
                "[%s went away; waiting for it]" % params.get("device"), file=sys.stderr
            )

    try:
        if wait:
            client.stream_debug_tee(
                device, baud, log_path, on_notify, duration, wait=wait, dtr=dtr
            )
        else:
            client.stream_debug_tee(device, baud, log_path, on_notify, duration)
    except KeyboardInterrupt:
        pass


def cmd_bootloader(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("bootloader"))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_usb_restart(ns: argparse.Namespace) -> None:
    """Re-enumerate an ESP32's USB node after `bootloader` wedges it (mpftp#31).

    No board connection and no elevation: this drives the SYSTEM scheduled task
    that does the privileged part. --status first, always -- the task on a given
    machine may not be the one this repo ships.
    """
    from . import espusb

    if ns.list:
        out({"devices": espusb.espressif_devices()})
        return
    if ns.status or not ns.instance:
        state = espusb.task_state()
        out(state)
        if not state["usable"]:
            raise SystemExit(1)
        return
    result = espusb.restart_device(ns.instance)
    out(result)
    if not result.get("ok"):
        # The task's own exit code, which is the whole report: 4 no such device,
        # 5 the restart failed, 6 it came back not OK. Printing `"ok": false` and
        # exiting 0 is the same defect this command exists to fix -- a caller
        # that branches on the exit code would go on to wait for a COM port
        # (mpftp#34).
        raise SystemExit(1)


def cmd_rtc(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        if ns.set:
            out(client.call("rtc_set"))
        else:
            out(client.call("rtc_get"))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_df(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("df"))
    finally:
        if mode.startswith("sidecar"):
            client.close()


# Debris left behind by agent iteration: scratch probe scripts, probe#capture
# targets, editor backups. Not everything an agent might ever write — a
# starting point tuned to what actually accumulates (mpftp#17).
DEFAULT_CLEAN_PATTERNS: tuple[str, ...] = (
    "probe_*.py",
    "*.probe.py",
    "result.txt",
    "*.bak",
    "__pycache__",
    "*.pyc",
)


def _find_debris(node: dict, patterns: list[str], out: list[dict]) -> None:
    """Walk an fs_tree() node, collecting matches. Does not descend into a
    matched directory — fs_rm_rf removes its whole subtree, so there is
    nothing further to find (or double-report) underneath it."""
    for child in node.get("children") or []:
        if any(fnmatch.fnmatch(child["name"], p) for p in patterns):
            out.append(child)
            continue
        if child.get("isDir"):
            _find_debris(child, patterns, out)


def cmd_clean(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        patterns = ns.pattern or list(DEFAULT_CLEAN_PATTERNS)
        tree = client.call("fs_tree", {"path": ns.path})
        matched: list[dict] = []
        _find_debris(tree, patterns, matched)
        if ns.dry_run:
            out(
                {
                    "dry_run": True,
                    "path": ns.path,
                    "patterns": patterns,
                    "matched": [{"path": m["path"], "isDir": m["isDir"]} for m in matched],
                }
            )
            return
        removed: list[str] = []
        errors: list[dict] = []
        for m in matched:
            try:
                client.call("fs_rm_rf" if m["isDir"] else "fs_rm", {"path": m["path"]})
                removed.append(m["path"])
            except Exception as e:
                errors.append({"path": m["path"], "error": str(e)})
        out({"dry_run": False, "path": ns.path, "patterns": patterns, "removed": removed, "errors": errors})
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_mip(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        params: dict[str, Any] = {
            "packages": ns.packages,
            "target": ns.target,
            "mpy": not ns.no_mpy,
        }
        if getattr(ns, "index", None):
            params["index"] = ns.index
        out(client.call("mip_install", params))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_circup(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        params: dict[str, Any] = {
            "packages": ns.packages,
            "target": ns.target or "/lib",
            "py": bool(ns.py),
            "prefer_web": not bool(ns.no_web),
        }
        if ns.host:
            params["host"] = ns.host
        if ns.password:
            params["password"] = ns.password
        out(client.call("circup_install", params))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_mount(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("mount", {"path": ns.path, "unsafe_links": ns.unsafe_links}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def _wifi_password(ns: argparse.Namespace, prompt: str) -> str:
    """The password for enable: --password, else asked for without echo."""
    import getpass

    password = getattr(ns, "password", None)
    if not password:
        password = getpass.getpass(prompt)
    try:
        return wifiboard.check_new_password(password)
    except ValueError as e:
        raise SystemExit(f"mpftp: {e}") from None


def _confirm(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_wifi_boards(ns: argparse.Namespace) -> None:
    rows = boards.listing()
    if ns.json:
        out(rows)
        return
    if not rows:
        print(
            "No boards remembered yet. Connect to one over serial while its Wi-Fi is up "
            "and mpftp records its address."
        )
        return
    for r in rows:
        pw = "password saved" if r["hasPassword"] else "no password saved"
        print(f"{r['name']:<20} {r['device']:<24} {r['uid']}  ({pw}, seen {r['seen']})")


def cmd_wifi_find(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        res = client.call("mdns_resolve", {"name": ns.name, "timeout": ns.timeout})
    finally:
        if mode.startswith("sidecar"):
            client.close()
    if ns.json:
        out(res)
    elif res.get("ip"):
        print(f"{res['name']} is at ws://{res['ip']} (found via {res['via']})")
    else:
        raise SystemExit(
            f"mpftp: nobody answered for {res['name']}. mDNS is best effort: from WSL "
            "it works only through the Windows sidecar, and some networks block it."
        )


def cmd_wifi_password(ns: argparse.Namespace) -> None:
    import getpass

    target = ns.board
    for row in boards.listing():
        if target in (row["name"], row["uid"]):
            target = row["uid"]
            break
    if ns.forget:
        print("forgotten" if boards.forget_password(target) else "no password was saved")
        return
    kind = "BLE REPL" if ble.is_ble_device(target) else "WebREPL"
    password = ns.password or getpass.getpass(f"{kind} password for {ns.board}: ")
    try:
        key = boards.set_password(target, password)
    except (webrepl.WebReplAuthError, ble.BleAuthError) as e:
        raise SystemExit(f"mpftp: {e}") from None
    print(f"saved for {key} in {boards._passwords_path()} (plaintext, mode 0600)")


def cmd_wifi_status(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        res = client.call("wifi_status")
        device = ns.device or ""
        boards.record_connect(device, res)
        out(res)
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_wifi_access(ns: argparse.Namespace) -> None:
    """Enable/Disable Wi-Fi access: show the boot.py change, ask, then write it."""
    action = ns.wifi_cmd
    client, mode = get_client()
    try:
        res = None
        if ns.device:
            res = connect_device(client, ns.device, ns.baud)
        password = None
        if action == "enable":
            password = _wifi_password(ns, "WebREPL password for this board (4-9 characters): ")
        plan = client.call("wifi_access_plan", {"action": action, "password": password})
        if plan.get("problems"):
            raise SystemExit("mpftp: " + " ".join(plan["problems"]))
        print(plan["diff"], end="")
        if action == "enable":
            print("(the password is shown as ***** here; the board gets the real one)")
        verb = "Delete" if plan.get("delete") else "Write this change to"
        if not ns.yes and not _confirm(f"{verb} {plan['path']} on the board?"):
            raise SystemExit("mpftp: nothing written")
        done = client.call(
            "wifi_access_apply",
            {
                "action": action,
                "password": password,
                "expect_sha256": plan.get("sha256"),
                "now": bool(ns.now),
            },
        )
        board = done.get("board") or (res or {}).get("board") or {}
        if action == "enable" and password and board.get("uid"):
            boards.set_password(board["uid"], password)
            boards.record_connect(ns.device or "", {"board": board})
        out(done)
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_umount(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("umount"))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_rpc(ns: argparse.Namespace) -> None:
    """Raw JSON-RPC: mpftp rpc METHOD [JSON_PARAMS]"""
    params = json.loads(ns.params) if ns.params else {}
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call(ns.method, params))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def resolve_build_python() -> str:
    """A native (Linux on WSL) python3 to run the firmware engine + make."""
    env = os.environ.get("MPFTP_BUILD_PYTHON")
    if env:
        return env
    import shutil

    if sys.platform != "win32":
        for cand in ("python3", "python"):
            p = shutil.which(cand)
            if p:
                return p
    return sys.executable or "python3"


def _engine_argv(cmd: str, extra: list[str]) -> list[str]:
    return [resolve_build_python(), *FIRMWARE_ENGINE, cmd, *extra]


def _engine_json(cmd: str, extra: list[str]) -> Any:
    r = subprocess.run(_engine_argv(cmd, extra), capture_output=True, text=True)
    if r.returncode != 0 and not r.stdout.strip():
        _die(r.stderr.strip() or f"engine {cmd} failed")
    try:
        return json.loads(r.stdout)
    except Exception:
        _die(r.stderr.strip() or r.stdout.strip() or f"engine {cmd}: bad output")


def _engine_stream(cmd: str, extra: list[str]) -> dict:
    """Run a streaming engine command; log lines -> stderr, return final result."""
    proc = subprocess.Popen(
        _engine_argv(cmd, extra),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    result: dict = {}
    assert proc.stdout
    for line in proc.stdout:
        line = line.rstrip("\n")
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            print(line, file=sys.stderr)
            continue
        if msg.get("type") == "log":
            print(msg.get("line", ""), file=sys.stderr)
        elif msg.get("type") == "result":
            result = msg
    proc.wait()
    return result or {"ok": proc.returncode == 0, "returncode": proc.returncode}


def _resolve_mp(ns: argparse.Namespace) -> Optional[str]:
    if getattr(ns, "mp", None):
        return ns.mp
    cwd = Path.cwd().resolve()
    workspace_hints = os.pathsep.join(str(path) for path in (cwd, *cwd.parents))
    info = _engine_json("discover", ["--workspace", workspace_hints])
    return info.get("micropython")


def _sel_args(ns: argparse.Namespace) -> list[str]:
    args: list[str] = []
    mp = _resolve_mp(ns)
    if mp:
        args += ["--mp", mp]
    if getattr(ns, "port", None):
        args += ["--port", ns.port]
    if getattr(ns, "board", None):
        args += ["--board", ns.board]
    if getattr(ns, "variant", None):
        args += ["--variant", ns.variant]
    for flag, attr in (
        ("--board-dir", "board_dir"),
        ("--variant-dir", "variant_dir"),
        ("--build-dir", "build_dir"),
        ("--module-roots", "module_roots"),
    ):
        if getattr(ns, attr, None):
            args += [flag, getattr(ns, attr)]
    return args


def _discovery_args(ns: argparse.Namespace) -> list[str]:
    extra = []
    mp = _resolve_mp(ns)
    if mp:
        extra += ["--mp", mp]
    if getattr(ns, "module_roots", None):
        extra += ["--module-roots", ns.module_roots]
    return extra


def format_modules(info: dict) -> str:
    """The module checklist as text: what --modules and --preset accept."""
    lines = []
    mods = info.get("modules") or []
    lines.append("Modules (pass names to --modules, comma-separated):")
    if not mods:
        lines.append("  none found")
    width = max([len(m["name"]) for m in mods] + [8])
    for m in mods:
        kind = "C" if m.get("hasC") else "freeze-only"
        needs = [r for r in m.get("requires") or [] if "/" not in r]
        tail = f"  needs {', '.join(needs)}" if needs else ""
        lines.append(f"  {m['name']:<{width}}  {kind:<11}  {m['path']}{tail}")
    presets = info.get("presets") or []
    if presets:
        lines.append("")
        lines.append("Presets (pass one name to --preset; add --modules for more):")
        width = max(len(p["name"]) for p in presets)
        for p in presets:
            if p.get("scansWorkspace"):
                what = "every module in the workspace"
            else:
                what = ", ".join(os.path.basename(os.path.dirname(r)) if "/" in r else r
                                 for r in p.get("requires") or []) or "upstream content only"
            lines.append(f"  {p['name']:<{width}}  {what}")
    roots = info.get("roots") or []
    if roots:
        lines.append("")
        lines.append("Scanned: " + ", ".join(roots))
        lines.append("Add roots with --module-roots or the firmwareModuleRoots setting.")
    return "\n".join(lines)


def format_tree(tree: dict, port: str, board: str, variant: str) -> Optional[str]:
    """Ports, then a port's boards or variants. None once a target is chosen."""
    ports = tree.get("ports") or []
    if not port:
        lines = [f"Ports in {tree.get('micropython')}:"]
        width = max([len(p["port"]) for p in ports] + [4])
        for p in ports:
            how = f"flash: {p['flasher']}" if p.get("flashable") else "build-only"
            lines.append(f"  {p['port']:<{width}}  {p['kind']:<8}  {how}")
        lines.append("")
        lines.append("Next: mpftp firmware list --port PORT")
        return "\n".join(lines)
    node = next((p for p in ports if p["port"] == port), None)
    if node is None:
        return f"No port named {port}."
    if node["kind"] == "boards" and not board:
        lines = [f"{port} boards:"]
        width = max([len(b["board"]) for b in node["boards"]] + [5])
        for b in node["boards"]:
            vs = f"variants: {', '.join(b['variants'])}" if b.get("variants") else ""
            src = f"  [{b['source']}]" if b.get("source") else ""
            lines.append(f"  {b['board']:<{width}}  {vs}{src}".rstrip())
        lines.append("")
        lines.append(f"Next: mpftp firmware list --port {port} --board BOARD")
        return "\n".join(lines)
    if node["kind"] == "variants" and not variant:
        sources = node.get("variantSources") or {}
        lines = [f"{port} variants:"]
        for v in node["variants"]:
            src = f"  [{sources[v]['source']}]" if v in sources else ""
            lines.append(f"  {v}{src}")
        lines.append("")
        lines.append(f"Next: mpftp firmware list --port {port} --variant VARIANT")
        return "\n".join(lines)
    return None


def cmd_firmware(ns: argparse.Namespace) -> None:
    sub = ns.fw_cmd
    if sub == "list":
        tree = _engine_json("tree", _discovery_args(ns))
        if getattr(ns, "json", False):
            out(tree)
            return
        text = format_tree(tree, ns.port or "", ns.board or "", ns.variant or "")
        if text is None:
            target = " ".join(
                f"--{k} {v}" for k, v in (("port", ns.port), ("board", ns.board),
                                          ("variant", ns.variant)) if v
            )
            text = (
                format_modules(_engine_json("modules", _discovery_args(ns)))
                + f"\n\nBuild: mpftp firmware build {target} --preset NAME --modules A,B"
            )
        print(text)
        return
    if sub == "discover":
        extra = ["--mp", ns.mp] if getattr(ns, "mp", None) else []
        out(_engine_json("discover", extra))
        return
    if sub in ("modules", "cmods"):
        info = _engine_json("modules", _discovery_args(ns))
        if sub == "cmods" or getattr(ns, "json", False):
            out(info)
        else:
            print(format_modules(info))
        return
    if sub == "artifact":
        out(_engine_json("artifact", _sel_args(ns)))
        return
    if sub == "ptable":
        extra = [ns.image]
        if getattr(ns, "compare", ""):
            extra += ["--compare", ns.compare]
        if getattr(ns, "device", ""):
            extra += ["--device", ns.device]
        out(_engine_json("ptable", extra))
        return
    if sub == "build":
        extra = _sel_args(ns)
        if ns.clean:
            extra.append("--clean")
        if getattr(ns, "preset", None):
            extra += ["--preset", ns.preset]
        if getattr(ns, "modules", None):
            extra += ["--modules", ns.modules]
        res = _engine_stream("build", extra)
        out(res)
        if not res.get("ok"):
            raise SystemExit(1)
        return
    if sub == "clean":
        res = _engine_stream("clean", _sel_args(ns))
        out(res)
        return
    if sub == "flash":
        extra = _sel_args(ns)
        if ns.device:
            extra += ["--device", ns.device]
        if getattr(ns, "artifact", None):
            extra += ["--artifact", ns.artifact]
        if getattr(ns, "family", None):
            extra += ["--family", ns.family]
        if getattr(ns, "erase", False):
            extra.append("--erase")
        if getattr(ns, "uf2", False):
            extra.append("--uf2")
        if getattr(ns, "uf2_timeout", 0):
            extra += ["--uf2-timeout", str(ns.uf2_timeout)]
        res = _engine_stream("flash", extra)
        out(res)
        if not res.get("ok"):
            raise SystemExit(1)
        return
    if sub == "download-tree":
        extra = []
        if getattr(ns, "force", False):
            extra.append("--force")
        out(_engine_json("download-tree", extra))
        return
    if sub == "download-list":
        extra = ["--board", ns.board]
        if getattr(ns, "variant", None):
            extra += ["--variant", ns.variant]
        if getattr(ns, "preview", False):
            extra.append("--preview")
        if getattr(ns, "force", False):
            extra.append("--force")
        out(_engine_json("download-list", extra))
        return
    if sub == "download":
        extra = ["--board", ns.board]
        if getattr(ns, "variant", None):
            extra += ["--variant", ns.variant]
        if getattr(ns, "version", None):
            extra += ["--version", ns.version]
        if getattr(ns, "preview", False):
            extra.append("--preview")
        if getattr(ns, "uf2", False):
            extra.append("--uf2")
        if getattr(ns, "force", False):
            extra.append("--force")
        res = _engine_stream("download", extra)
        out(res)
        if not res.get("ok"):
            raise SystemExit(1)
        return
    if sub == "detect":
        extra = []
        mp = _resolve_mp(ns)
        if mp:
            extra += ["--mp", mp]
        extra += ["--device", ns.device]
        if getattr(ns, "baud", None):
            extra += ["--baud", str(ns.baud)]
        if getattr(ns, "mp_hints", None):
            extra += ["--mp-hints", ns.mp_hints]
        out(_engine_json("detect", extra))
        return
    if sub == "partitions":
        extra = [ns.part_action]
        mp = _resolve_mp(ns)
        if mp:
            extra += ["--mp", mp]
        if getattr(ns, "board", None):
            extra += ["--board", ns.board]
        if getattr(ns, "variant", None):
            extra += ["--variant", ns.variant]
        if ns.part_action == "set":
            if getattr(ns, "csv_file", None):
                extra += ["--csv-file", ns.csv_file]
            elif getattr(ns, "rows", None):
                extra += ["--rows", ns.rows]
            else:
                _die("partitions set requires --csv-file or --rows")
        elif ns.part_action == "split":
            if getattr(ns, "storage_bytes", None):
                extra += ["--storage-bytes", str(ns.storage_bytes)]
            if getattr(ns, "flash_bytes", None):
                extra += ["--flash-bytes", str(ns.flash_bytes)]
            if getattr(ns, "flash_mb", None):
                extra += ["--flash-mb", str(ns.flash_mb)]
        out(_engine_json("partitions", extra))
        return
    _die(f"unknown firmware command: {sub}")


def cmd_watch_repl(ns: argparse.Namespace) -> None:
    """Live-tail the board's own stdout without ever interrupting it (mpftp#10).

    Unlike `get`/`exec`/`put`, this never enters raw REPL — no Ctrl-C is ever
    sent, so a running script keeps running. It only shows what the script
    itself prints; it cannot read an arbitrary board file (that fundamentally
    requires raw REPL). Ctrl-C here stops *watching*, not the board.
    """
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        print(
            "watching board stdout (Ctrl-C stops watching, not the board) ...",
            file=sys.stderr,
        )

        def on_notify(method: str, params: dict) -> None:
            if method == "repl_data":
                b64 = params.get("data_b64")
                if b64:
                    sys.stdout.buffer.write(base64.b64decode(b64))
                    sys.stdout.buffer.flush()
            elif method == "repl_error":
                print(f"[repl_error] {params.get('message')}", file=sys.stderr)

        try:
            client.stream_repl(on_notify)
        except KeyboardInterrupt:
            pass
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_watch(ns: argparse.Namespace) -> None:
    path = Path(ns.file) if ns.file else (REPL_LOG if ns.repl else ACTIVITY_LOG)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    print(f"watching {path}", file=sys.stderr)
    with path.open("r", encoding="utf-8", errors="replace") as f:
        if not ns.from_start:
            f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if line:
                sys.stdout.write(line)
                sys.stdout.flush()
            else:
                time.sleep(0.25)


def build_parser() -> argparse.ArgumentParser:
    device_opts = argparse.ArgumentParser(add_help=False)
    device_opts.add_argument(
        "--device",
        "-d",
        dest="device",
        default=None,
        help="Serial port (COM4, /dev/ttyACM0), WebREPL address (ws://HOST[:8266]) or "
        "bledev.repl board (ble://NAME); a ws:// device reads its password from "
        "MPFTP_WEBREPL_PASSWORD or webreplPassword in ~/.mpftp/config.json, a ble:// "
        "one from MPFTP_BLE_PASSWORD or blePassword",
    )
    device_opts.add_argument("--baud", type=int, default=config.resolve("defaultBaud"))

    p = argparse.ArgumentParser(prog="mpftp", description="mpftp agent CLI (mpremote via sidecar)")
    p.add_argument("--version", action="version", version=f"mpftp {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="RPC socket + session status").set_defaults(func=cmd_status)
    sub.add_parser("ports", parents=[device_opts], help="List serial ports").set_defaults(func=cmd_ports)

    c = sub.add_parser("connect", parents=[device_opts], help="Connect to device")
    c.add_argument(
        "device_pos", metavar="DEVICE", help="e.g. COM4, /dev/ttyACM0, ws://192.168.1.50:8266 or ble://rack"
    )
    c.set_defaults(func=cmd_connect)

    sub.add_parser("disconnect", parents=[device_opts], help="Disconnect").set_defaults(func=cmd_disconnect)
    sub.add_parser("resume", parents=[device_opts], help="Reconnect to last device").set_defaults(
        func=cmd_resume
    )

    ls = sub.add_parser("ls", parents=[device_opts], help="List board directory")
    ls.add_argument("path", nargs="?", default="/")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_ls)

    tr = sub.add_parser("tree", parents=[device_opts], help="Tree board directory")
    tr.add_argument("path", nargs="?", default="/")
    tr.set_defaults(func=cmd_tree)

    put = sub.add_parser("put", parents=[device_opts], help="Upload local file to board")
    put.add_argument("local")
    put.add_argument("remote")
    put.add_argument("-r", "--recursive", action="store_true", help="Copy directories via fs_cp")
    put.add_argument(
        "--verify",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="SHA-256 verify after transfer (default: on; --no-verify to skip)",
    )
    put.add_argument(
        "--mpy",
        "--compile",
        dest="mpy",
        action="store_true",
        help="Compile .py to .mpy via mpy-cross before uploading (MicroPython only; "
        "boot.py/main.py are never compiled)",
    )
    put.set_defaults(func=cmd_put)

    get = sub.add_parser("get", parents=[device_opts], help="Download board file to local")
    get.add_argument("remote")
    get.add_argument("local")
    get.add_argument("-r", "--recursive", action="store_true", help="Copy directories via fs_cp")
    get.add_argument(
        "--verify",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="SHA-256 verify after transfer (default: on; --no-verify to skip)",
    )
    get.set_defaults(func=cmd_get)

    cp = sub.add_parser(
        "cp",
        parents=[device_opts],
        help="Copy (use : prefix for board paths, e.g. ./a.py :/a.py)",
    )
    cp.add_argument("src")
    cp.add_argument("dest")
    cp.add_argument(
        "--verify",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="SHA-256 verify after transfer (default: on; --no-verify to skip)",
    )
    cp.add_argument(
        "--mpy",
        "--compile",
        dest="mpy",
        action="store_true",
        help="Compile .py to .mpy via mpy-cross on local->board copies (MicroPython "
        "only; boot.py/main.py are never compiled)",
    )
    cp.set_defaults(func=cmd_cp)

    hx = sub.add_parser("hash", parents=[device_opts], help="SHA-256 (or algo) of board file")
    hx.add_argument("path")
    hx.add_argument("--algo", default="sha256")
    hx.set_defaults(func=cmd_hash)

    ed = sub.add_parser("edit", parents=[device_opts], help="Edit board file with $EDITOR")
    ed.add_argument("path")
    ed.set_defaults(func=cmd_edit)

    mk = sub.add_parser("mkdir", parents=[device_opts], help="Create board directory")
    mk.add_argument("path")
    mk.set_defaults(func=cmd_mkdir)

    rm = sub.add_parser("rm", parents=[device_opts], help="Remove board file (or -r tree)")
    rm.add_argument("path")
    rm.add_argument("-r", "--recursive", action="store_true")
    rm.set_defaults(func=cmd_rm)

    touch = sub.add_parser("touch", parents=[device_opts], help="Create empty board file")
    touch.add_argument("path")
    touch.set_defaults(func=cmd_touch)

    ren = sub.add_parser("rename", parents=[device_opts], help="Rename board path")
    ren.add_argument("src")
    ren.add_argument("dest")
    ren.set_defaults(func=cmd_rename)

    ev = sub.add_parser("eval", parents=[device_opts], help="Eval expression on board")
    ev.add_argument("expr")
    ev.set_defaults(func=cmd_eval)

    ex = sub.add_parser("exec", parents=[device_opts], help="Exec code on board")
    ex.add_argument("code")
    ex.add_argument(
        "--no-follow",
        action="store_true",
        help="Do not wait for raw-REPL EOF (use for long-running / UI code)",
    )
    ex.set_defaults(func=cmd_exec)

    run = sub.add_parser(
        "run",
        parents=[device_opts],
        help="Run local script on board (default: --no-follow)",
    )
    run.add_argument("file")
    run.add_argument(
        "--follow",
        action="store_true",
        help="Wait for the script to finish (default is no-follow for UI apps)",
    )
    run.set_defaults(func=cmd_run)

    pb = sub.add_parser(
        "probe",
        parents=[device_opts],
        help="Run a script, wait, and capture a result file in one shot",
    )
    pb.add_argument("file")
    pb.add_argument(
        "--reboot-first",
        action="store_true",
        help="Hard-reset and reconnect before running (clears stale module state; requires --device)",
    )
    pb.add_argument("--capture", metavar="PATH", help="Board file to read back after --wait")
    pb.add_argument(
        "--wait", type=float, default=0.0, metavar="SECONDS", help="Wait this long before capturing"
    )
    pb.set_defaults(func=cmd_probe)

    sub.add_parser(
        "interrupt",
        parents=[device_opts],
        help="Send Ctrl-C (interrupt running program; no reset)",
    ).set_defaults(func=cmd_interrupt)
    sub.add_parser(
        "soft-reset",
        parents=[device_opts],
        help="Soft reset (MP: skip main.py; CP: friendly↔raw, does not run code.py)",
    ).set_defaults(func=cmd_soft_reset)
    sub.add_parser(
        "soft-reboot",
        parents=[device_opts],
        help="Friendly Ctrl-D soft-reboot (runs main.py / code.py)",
    ).set_defaults(func=cmd_soft_reboot)
    hr = sub.add_parser(
        "hard-reset",
        parents=[device_opts],
        help="Hard reset; the board boots normally and runs main.py / code.py",
    )
    hr.add_argument(
        "--monitor",
        type=float,
        metavar="SECONDS",
        default=None,
        help="After the reset, wait for the same port to come back (native USB "
        "re-enumerates) and stream the boot read-only, as `monitor` does, for "
        "SECONDS. No REPL, no DTR/RTS, so main.py keeps running.",
    )
    hr.add_argument(
        "--log-path",
        help="With --monitor: append the raw console bytes here too",
    )
    hr.set_defaults(func=cmd_hard_reset)
    sub.add_parser("bootloader", parents=[device_opts], help="Enter bootloader").set_defaults(
        func=cmd_bootloader
    )

    ur = sub.add_parser(
        "usb-restart",
        help="Windows: re-enumerate an ESP32 USB node wedged by `bootloader` (no elevation)",
    )
    ur.add_argument("--instance", help="Device instance id, e.g. 'USB\\VID_303A&PID_4003\\<serial>'")
    ur.add_argument("--list", action="store_true", help="List attached VID_303A devices and their instance ids")
    ur.add_argument(
        "--status",
        action="store_true",
        help="Report whether the no-UAC recovery task is installed and would work; exit 1 if not",
    )
    ur.set_defaults(func=cmd_usb_restart)

    dtee = sub.add_parser(
        "debug-tee",
        help="Read-only monitor on a second COM (e.g. ESP native USB CDC)",
    )
    dtee.add_argument(
        "device_tee",
        nargs="?",
        help="Second serial device (required unless --stop)",
    )
    dtee.add_argument("--baud", type=int, default=config.resolve("defaultBaud"))
    dtee.add_argument("--log-path", help="Append raw bytes (default ~/.mpftp/debug-tee.log)")
    dtee.add_argument("--stop", action="store_true", help="Stop an active debug tee")
    dtee.set_defaults(func=cmd_debug_tee)

    mon = sub.add_parser(
        "monitor",
        help="Read-only console capture on a COM, held open for --seconds "
        "(unlike debug-tee, does not die when the command returns)",
    )
    mon.add_argument(
        "device_mon",
        help="Serial device carrying the firmware console (e.g. COM4 or the "
        "native USB CDC debug port). Never enters REPL, never toggles DTR/RTS.",
    )
    mon.add_argument("--baud", type=int, default=config.resolve("defaultBaud"))
    mon.add_argument(
        "--seconds",
        type=float,
        default=None,
        help="Capture for this many seconds, then stop (default: until Ctrl-C)",
    )
    mon.add_argument(
        "--log-path",
        help="Append raw bytes here too (default: stdout only)",
    )
    mon.set_defaults(func=cmd_monitor)

    rtc = sub.add_parser("rtc", parents=[device_opts], help="Get or set RTC")
    rtc.add_argument("--set", action="store_true", help="Set RTC from the host clock, in UTC")
    rtc.set_defaults(func=cmd_rtc)

    sub.add_parser("df", parents=[device_opts], help="Disk free").set_defaults(func=cmd_df)

    cl = sub.add_parser(
        "clean",
        parents=[device_opts],
        help="Remove board debris (probe scratch files, .bak, __pycache__, ...)",
    )
    cl.add_argument("path", nargs="?", default="/")
    cl.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be removed without deleting anything",
    )
    cl.add_argument(
        "--pattern",
        action="append",
        help="Glob pattern to match (repeatable). Default: "
        + ", ".join(DEFAULT_CLEAN_PATTERNS),
    )
    cl.set_defaults(func=cmd_clean)

    mip = sub.add_parser("mip", parents=[device_opts], help="mip install package(s) (MicroPython)")
    mip.add_argument("packages", nargs="+")
    mip.add_argument(
        "--target",
        default="/lib",
        help="Board install directory (default: /lib)",
    )
    mip.add_argument("--no-mpy", action="store_true")
    mip.add_argument(
        "--index",
        default=None,
        help="Package index base URL (default: micropython.org/pi/v2)",
    )
    mip.set_defaults(func=cmd_mip)

    circ = sub.add_parser(
        "circup",
        parents=[device_opts],
        help="circup install package(s) (prefers Web Workflow when Wi-Fi is up)",
    )
    circ.add_argument("packages", nargs="+")
    circ.add_argument("--target", default="/lib")
    circ.add_argument(
        "--py",
        action="store_true",
        help="Install .py sources instead of .mpy",
    )
    circ.add_argument("--host", help="Web Workflow host/IP (default: probe board Wi-Fi)")
    circ.add_argument(
        "--password",
        help="Web Workflow password (default: CIRCUITPY_WEB_API_PASSWORD / env)",
    )
    circ.add_argument(
        "--no-web",
        action="store_true",
        help="Skip Web Workflow; use USB staging/serial/MSC only",
    )
    circ.set_defaults(func=cmd_circup)

    mnt = sub.add_parser("mount", parents=[device_opts], help="Mount local path on board (MicroPython)")
    mnt.add_argument("path")
    mnt.add_argument("--unsafe-links", action="store_true")
    mnt.set_defaults(func=cmd_mount)
    sub.add_parser("umount", parents=[device_opts], help="Umount local mount (MicroPython)").set_defaults(func=cmd_umount)

    wifi = sub.add_parser(
        "wifi",
        help="Reach boards over Wi-Fi: remembered boards, passwords, boot.py setup, mDNS",
    )
    wsub = wifi.add_subparsers(dest="wifi_cmd", required=True)
    wb = wsub.add_parser("boards", help="Boards remembered from serial connections")
    wb.add_argument("--json", action="store_true")
    wb.set_defaults(func=cmd_wifi_boards)
    wf = wsub.add_parser("find", help="Look up NAME.local by mDNS (best effort)")
    wf.add_argument("name", help="e.g. mpy-esp32p4 or mpy-esp32p4.local")
    wf.add_argument("--timeout", type=float, default=1.5)
    wf.add_argument("--json", action="store_true")
    wf.set_defaults(func=cmd_wifi_find)
    wp = wsub.add_parser(
        "password",
        help="Save a board's WebREPL password in ~/.mpftp/webrepl-passwords.json (plaintext, 0600)",
    )
    wp.add_argument("board", help="remembered name or uid, ws://HOST, or ble://NAME")
    wp.add_argument("--password", help="default: ask without echo")
    wp.add_argument("--forget", action="store_true")
    wp.set_defaults(func=cmd_wifi_password)
    ws = wsub.add_parser("status", parents=[device_opts], help="The board's uid, hostname and IP")
    ws.set_defaults(func=cmd_wifi_status)
    for name, text in (
        ("enable", "Add mpftp's Wi-Fi block to boot.py (serial connection; shows the change first)"),
        ("disable", "Remove mpftp's Wi-Fi block from boot.py (shows the change first)"),
    ):
        we = wsub.add_parser(name, parents=[device_opts], help=text)
        if name == "enable":
            we.add_argument("--password", help="4-9 characters; default: ask without echo")
        we.add_argument("--yes", action="store_true", help="Write without asking")
        we.add_argument(
            "--now",
            action="store_true",
            help="Also start (enable) or stop (disable) WebREPL now, without a reset",
        )
        we.set_defaults(func=cmd_wifi_access)

    rom = sub.add_parser("romfs", parents=[device_opts], help="ROMFS query/build/deploy (MicroPython)")
    rom.add_argument("romfs_cmd", choices=["query", "build", "deploy"])
    rom.add_argument("path", nargs="?", help="Source dir or .romfs image (build/deploy)")
    rom.add_argument("-o", "--output", help="Output file for build")
    rom.add_argument("--partition", type=int, default=0)
    rom.add_argument("--no-mpy", action="store_true")
    rom.set_defaults(func=cmd_romfs)

    rpc = sub.add_parser("rpc", parents=[device_opts], help="Raw RPC method")
    rpc.add_argument("method")
    rpc.add_argument("params", nargs="?", help='JSON object, e.g. {"path":"/"}')
    rpc.set_defaults(func=cmd_rpc)

    fw = sub.add_parser("firmware", help="Build & flash MicroPython firmware (host-side)")
    fwsub = fw.add_subparsers(dest="fw_cmd", required=True)

    fw_sel = argparse.ArgumentParser(add_help=False)
    fw_sel.add_argument("--mp", help="MicroPython tree path (auto-discovered if omitted)")
    fw_sel.add_argument("--port", help="MicroPython port, e.g. esp32")
    fw_sel.add_argument("--board", default="", help="Board name")
    fw_sel.add_argument("--variant", default="", help="Board/port variant")
    fw_sel.add_argument("--board-dir", dest="board_dir", default="",
                        help="Board directory outside the port (found automatically for overlays)")
    fw_sel.add_argument("--variant-dir", dest="variant_dir", default="",
                        help="Variant directory outside the port (found automatically for overlays)")
    fw_sel.add_argument("--build-dir", dest="build_dir", default="",
                        help="Build directory (default: the port's build-<target>)")
    fw_sel.add_argument("--module-roots", dest="module_roots", default="",
                        help=f"Extra directories to scan for modules, {os.pathsep!r}-separated")

    fwl = fwsub.add_parser(
        "list",
        parents=[fw_sel],
        help="Ports; with --port its boards/variants; with a board or variant, its modules",
    )
    fwl.add_argument("--json", action="store_true", help="The whole tree as JSON")
    fwl.set_defaults(func=cmd_firmware)
    fwm = fwsub.add_parser(
        "modules", parents=[fw_sel], help="Modules and presets a build can include"
    )
    fwm.add_argument("--json", action="store_true", help="Machine-readable output")
    fwm.set_defaults(func=cmd_firmware)
    fwsub.add_parser("discover", parents=[fw_sel], help="Show resolved MP/IDF/emsdk paths").set_defaults(
        func=cmd_firmware
    )
    fwsub.add_parser("cmods", parents=[fw_sel], help="Old name for modules --json").set_defaults(
        func=cmd_firmware
    )
    fwsub.add_parser("artifact", parents=[fw_sel], help="Report built firmware for a selection").set_defaults(
        func=cmd_firmware
    )

    fwb = fwsub.add_parser("build", parents=[fw_sel], help="Build firmware (streams log)")
    fwb.add_argument("--clean", action="store_true", help="Clean before building")
    fwb.add_argument("--preset", default="",
                     help="Saved selection to start from (see firmware modules), or a manifest path")
    fwb.add_argument("--modules", default="",
                     help="Modules to add, comma-separated names or paths (see firmware modules)")
    fwb.set_defaults(func=cmd_firmware)

    fwsub.add_parser("clean", parents=[fw_sel], help="Clean a selection").set_defaults(
        func=cmd_firmware
    )

    fwp = fwsub.add_parser(
        "ptable", help="Print a firmware image's partition table; diff two, or a board's"
    )
    fwp.add_argument("image", help="Firmware .bin (whole-flash image)")
    fwp.add_argument("--compare", default="", help="Second image to diff against")
    fwp.add_argument("--device", default="", help="Also read and diff this board's table")
    fwp.set_defaults(func=cmd_firmware)

    fwf = fwsub.add_parser("flash", parents=[fw_sel, device_opts], help="Flash a built or downloaded artifact")
    fwf.add_argument("--artifact", help="Explicit firmware file (else last build)")
    fwf.add_argument("--family", default="", help="MCU family for flash offset (download mode)")
    fwf.add_argument("--erase", action="store_true", help="esp32: erase flash first")
    fwf.add_argument("--uf2", action="store_true",
                     help="Copy a .uf2 to a bootloader volume instead of flashing over serial")
    fwf.add_argument("--uf2-timeout", dest="uf2_timeout", type=float, default=0.0,
                     help="Seconds to wait for the volume to unmount (default 30)")
    fwf.set_defaults(func=cmd_firmware)

    fwdt = fwsub.add_parser(
        "download-tree", help="Official firmware catalog (Thonny JSON → micropython.org)"
    )
    fwdt.add_argument("--force", action="store_true", help="Refresh catalog cache")
    fwdt.set_defaults(func=cmd_firmware)

    fwdlist = fwsub.add_parser("download-list", help="List downloadable versions for a board")
    fwdlist.add_argument("--board", required=True)
    fwdlist.add_argument(
        "--variant",
        default="",
        help="MP board variant (e.g. C6_WIFI)",
    )
    fwdlist.add_argument("--preview", action="store_true", help="Probe board page for latest preview")
    fwdlist.add_argument("--force", action="store_true")
    fwdlist.set_defaults(func=cmd_firmware)

    fwdd = fwsub.add_parser("download", help="Download official firmware for a board")
    fwdd.add_argument("--board", required=True)
    fwdd.add_argument(
        "--variant",
        default="",
        help="MP board variant (e.g. C6_WIFI)",
    )
    fwdd.add_argument("--version", default="", help="Release version (e.g. 1.28.0)")
    fwdd.add_argument("--preview", action="store_true", help="Latest preview build")
    fwdd.add_argument(
        "--uf2",
        action="store_true",
        help="Prefer .uf2 (default: .bin for esp32 serial, .uf2 for rp2/samd)",
    )
    fwdd.add_argument("--force", action="store_true", help="Refresh catalog cache")
    fwdd.set_defaults(func=cmd_firmware)

    fwd = fwsub.add_parser("detect", parents=[fw_sel, device_opts],
                           help="esptool-first chip/flash/security probe")
    fwd.add_argument("--mp-hints", dest="mp_hints",
                     help="JSON of MicroPython interpreter hints (optional)")
    fwd.set_defaults(func=cmd_firmware)

    fwp = fwsub.add_parser("partitions", parents=[fw_sel], help="esp32 partition override")
    fwp.add_argument("part_action", choices=["get", "set", "reset", "candidates", "split"])
    fwp.add_argument("--rows", help="JSON array of partition rows (set)")
    fwp.add_argument("--csv-file", dest="csv_file", help="CSV file to import (set)")
    fwp.add_argument("--storage-bytes", dest="storage_bytes", type=int,
                     help="storage partition size in bytes (split)")
    fwp.add_argument("--flash-bytes", dest="flash_bytes", type=int,
                     help="total flash in bytes (split)")
    fwp.add_argument("--flash-mb", dest="flash_mb", type=int,
                     help="flash size in MB for the sdkconfig fragment (split)")
    fwp.set_defaults(func=cmd_firmware)

    w = sub.add_parser("watch", help="Tail activity or REPL log")
    w.add_argument("--repl", action="store_true", help="Watch REPL log instead of activity")
    w.add_argument("--file", help="Custom log path")
    w.add_argument("--from-start", action="store_true")
    w.set_defaults(func=cmd_watch)

    wr = sub.add_parser(
        "watch-repl",
        parents=[device_opts],
        help="Non-interrupting live tail of the board's own stdout (never sends Ctrl-C)",
    )
    wr.set_defaults(func=cmd_watch_repl)

    return p


def main(argv: Optional[list[str]] = None) -> None:
    parser = build_parser()
    ns = parser.parse_args(argv)
    if getattr(ns, "cmd", None) == "connect":
        ns.device = ns.device_pos
    elif not hasattr(ns, "device"):
        ns.device = None
    if not hasattr(ns, "baud"):
        ns.baud = 115200
    try:
        ns.func(ns)
    except BrokenPipeError:
        pass
    except SystemExit:
        raise
    except Exception as e:
        _emit_error_envelope(e)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
