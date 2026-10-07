"""``mpftp hold``: keep one board's friendly REPL open between commands.

Every other mpftp command opens a connection, enters the raw REPL with a
Ctrl-C, does its work and lets go. That stops whatever the board was running.
A holder is the other way round: it opens the connection once, sends nothing
of its own, and stays. Each ``mpftp hold`` command then types into the
friendly REPL as a person would, and reads back what the board printed, while
the app the board is running keeps running.

Three processes take part:

- the **client**, each ``mpftp hold ...`` command, which lives for one call;
- the **holder** (``python -m mpftp.hold daemon``), detached, which owns the
  state directory (``~/.mpftp/holds/<key>/``), the PID file, the log and a
  socket on 127.0.0.1 that only clients holding its token can use;
- the **pump** (``python -m mpftp.hold pump``), the holder's child and the only
  process that touches the device. It copies its stdin to the device and the
  device's output to its stdout, and it stops when its stdin closes. On WSL
  it runs under Windows Python for a COM port, a ws:// board and
  ``micropython.exe``, the way the sidecar does, so the holder itself stays a
  Linux process with a Linux PID.

What a device can be:

- a serial port (``COM42``, ``/dev/ttyACM0``), opened the way mpremote opens
  it, which on a native-USB board resets nothing;
- a WebREPL address (``ws://HOST[:8266]``), password from
  ``~/.mpftp/webrepl-passwords.json`` like every other ws:// command;
- a desktop interpreter started for the purpose (``--spawn``): on a pseudo
  terminal on Linux and macOS, and inside a Windows pseudo console (ConPTY)
  for a Windows ``.exe``, because ``micropython.exe`` reads the console API
  and spins on a plain pipe (micropython-pydevices#9).

Nothing here soft-resets the board or enters the raw REPL, except
``hold exec``, which is the raw REPL on purpose.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import config

HOLDS_DIR = config.CONFIG_DIR / "holds"

STREAM = "stream.bin"  # every byte the device printed, as it came
TRANSCRIPT = "transcript.log"  # both directions, one timestamped line each
STATE = "hold.json"  # how to reach the holder; mode 0600 (has its token)
PIDFILE = "hold.pid"
CURSOR = "cursor"  # where `hold read` stopped last time
RAW_FLAG = "raw-pending"  # `hold exec` left the raw REPL entered

PROMPT = ">>> "
CONTINUATION = "... "
PASTE_PROMPT = "=== "
RAW_BANNER = "raw REPL; CTRL-B to exit\n>"

READY = "MPFTP-HOLD-READY"
ERROR = "MPFTP-HOLD-ERROR"
ENDED = "MPFTP-HOLD-ENDED"

# ConPTY's own escapes: colours, cursor moves, title, mode switches. Once its
# screen is full it writes runs of spaces as cursor-forward (ESC[nC), which
# become spaces again before the rest are dropped.
_VT_SPACES = re.compile(rb"\x1b\[(\d*)[CX]")
_VT = re.compile(
    rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[=>()][0-9A-Za-z]?"
)
_VT_PARTIAL = re.compile(rb"\x1b(?:\[[0-9;?]*[ -/]*|\][^\x07\x1b]*|[()]?)?$")


def vt_to_text(data: bytes) -> tuple[bytes, bytes]:
    """A pseudo console's VT stream as plain text: (text, unfinished escape to carry)."""
    carry = b""
    m = _VT_PARTIAL.search(data)
    if m and m.start() >= max(0, len(data) - 64):
        carry, data = data[m.start() :], data[: m.start()]
    data = _VT_SPACES.sub(lambda m: b" " * int(m.group(1) or b"1"), data)
    return _VT.sub(b"", data), carry


class HoldError(RuntimeError):
    """A hold command that could not do what was asked; the message says why."""


# ---------------------------------------------------------------------------
# Keys and state
# ---------------------------------------------------------------------------


def is_windows_exe(argv0: str) -> bool:
    return argv0.lower().endswith(".exe")


def key_for(device: str) -> str:
    """The state directory name for a device or a spawned interpreter's name."""
    d = device.strip()
    if re.fullmatch(r"(?i)com\d+", d):
        return d.upper()
    d = re.sub(r"^ws://", "ws-", d)
    d = d.replace("/dev/", "dev-")
    d = re.sub(r"[^A-Za-z0-9._-]+", "-", d).strip("-")
    return d or "hold"


def spawn_name(argv: list[str]) -> str:
    base = re.split(r"[\\/]", argv[0])[-1]
    return key_for(base.replace(".exe", "-exe") if is_windows_exe(base) else base)


def hold_dir(key: str) -> Path:
    return HOLDS_DIR / key


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    cmdline = Path(f"/proc/{pid}/cmdline")
    if cmdline.exists():
        # A PID the OS gave to something else since is not our holder.
        try:
            return b"mpftp.hold" in cmdline.read_bytes()
        except OSError:
            return True
    return True


def holder_pid(key: str) -> Optional[int]:
    """The live holder's PID for ``key``, or None (clearing a stale PID file)."""
    pidfile = hold_dir(key) / PIDFILE
    try:
        text = pidfile.read_text().strip()
    except OSError:
        return None
    if text.startswith("starting"):
        # A start in progress; stale after 30 s.
        try:
            if time.time() - pidfile.stat().st_mtime < 30:
                return -1
        except OSError:
            return None
        pidfile.unlink(missing_ok=True)
        return None
    try:
        pid = int(text)
    except ValueError:
        pidfile.unlink(missing_ok=True)
        return None
    if pid_alive(pid):
        return pid
    pidfile.unlink(missing_ok=True)
    return None


def list_holds() -> list[dict[str, Any]]:
    out = []
    if not HOLDS_DIR.is_dir():
        return out
    for d in sorted(HOLDS_DIR.iterdir()):
        if not d.is_dir():
            continue
        st = _read_json(d / STATE)
        pid = holder_pid(d.name)
        out.append(
            {
                "key": d.name,
                "device": st.get("device"),
                "live": bool(pid),
                "pid": pid if pid and pid > 0 else None,
                "state": st.get("state") if pid else (st.get("state") or "stopped"),
                "started": st.get("started"),
            }
        )
    return out


def held_by(device: Optional[str]) -> Optional[int]:
    """The PID holding ``device``, if a live holder has it (for other commands)."""
    if not device:
        return None
    pid = holder_pid(key_for(device))
    return pid if pid and pid > 0 else None


def resolve_key(target: Optional[str]) -> str:
    """Which holder a command means: ``-d`` as a device or a name, else the only live one."""
    if target:
        for key in (key_for(target), target):
            if (hold_dir(key) / STATE).exists() or holder_pid(key):
                return key
        raise HoldError(
            f"nothing is holding {target}; start one with `mpftp hold start -d {target}`"
        )
    live = [h["key"] for h in list_holds() if h["live"]]
    if len(live) == 1:
        return live[0]
    if not live:
        raise HoldError("no holder is running; start one with `mpftp hold start -d DEVICE`")
    raise HoldError("more than one holder is running (" + ", ".join(live) + "); say which with -d")


# ---------------------------------------------------------------------------
# Reading the stream
# ---------------------------------------------------------------------------


def stream_size(key: str) -> int:
    try:
        return (hold_dir(key) / STREAM).stat().st_size
    except OSError:
        return 0


def read_stream(key: str, start: int, end: Optional[int] = None) -> bytes:
    try:
        with open(hold_dir(key) / STREAM, "rb") as f:
            f.seek(start)
            return f.read() if end is None else f.read(max(0, end - start))
    except OSError:
        return b""


def clean(data: bytes) -> str:
    """Text as an agent wants it: decoded, no carriage returns."""
    return data.decode("utf-8", "replace").replace("\r", "")


def _line_start_prompt(text: str, start: int) -> int:
    """Index of the first friendly prompt at a line start at or after ``start``, or -1."""
    i = start
    while True:
        i = text.find(PROMPT, i)
        if i < 0:
            return -1
        if i == 0 or text[i - 1] == "\n":
            return i
        i += 1


def parse_reply(text: str, echo: Optional[str], after: Optional[str] = None) -> dict[str, Any]:
    """Split what came back after a line was typed.

    ``text`` is everything the device printed since the send (carriage returns
    removed). The reply starts after the echo of what was typed (or after the
    last ``after`` marker, for paste mode), so a prompt that was already on its
    way from an earlier command doesn't count, and runs to the first prompt at
    a line start after that. App output in between stays in ``output``.
    """
    start = 0
    if after:
        k = text.rfind(after)
        if k >= 0:
            start = text.find("\n", k)
            start = len(text) if start < 0 else start + 1
    elif echo:
        k = text.find(echo)
        if k >= 0:
            nl = text.find("\n", k + len(echo))
            start = len(text) if nl < 0 else nl + 1
    p = _line_start_prompt(text, start)
    if p < 0:
        tail = text[start:]
        last = tail.rsplit("\n", 1)[-1]
        return {
            "prompt": False,
            # "... " and any auto-indent after it.
            "continuation": last.startswith(CONTINUATION.rstrip()) and not last[3:].strip(),
            "output": tail,
            "consumed": len(text),
        }
    return {"prompt": True, "output": text[start:p], "consumed": p + len(PROMPT)}


def marked(output: str, marker: str) -> list[str]:
    """The text after ``marker`` on each line that has it."""
    found = []
    for line in output.split("\n"):
        k = line.find(marker)
        if k >= 0:
            found.append(line[k + len(marker) :].strip())
    return found


def parse_raw_reply(text: str) -> Optional[dict[str, Any]]:
    """A raw-REPL exec's result once complete: ``OK<out>\\x04<err>\\x04>``."""
    k = text.find("OK")
    if k < 0:
        return None
    rest = text[k + 2 :]
    a = rest.find("\x04")
    if a < 0:
        return None
    b = rest.find("\x04", a + 1)
    if b < 0:
        return None
    return {"output": rest[:a], "error": rest[a + 1 : b]}


# ---------------------------------------------------------------------------
# Endpoints (run in the pump)
# ---------------------------------------------------------------------------


class SerialEnd:
    """A serial port, opened as mpremote opens it (no reset on native USB)."""

    def __init__(self, spec: dict[str, Any]) -> None:
        import serial

        port = spec["port"]
        kwargs: dict[str, Any] = {"baudrate": int(spec.get("baud") or 115200), "timeout": 0.05}
        if serial.__version__ >= "3.3":
            kwargs["exclusive"] = True
        self.s = serial.serial_for_url(port, do_not_open=True, **kwargs)
        if os.name == "nt":
            import serial.tools.list_ports

            info = list(serial.tools.list_ports.grep(port))
            if info and getattr(info[0], "vid", None) == 0x10C4:
                # mpremote's CP210x quirk: set the lines in an order that
                # never pulses an Espressif board's reset.
                self.s.dtr = False
                self.s.rts = False
                self.s.open()
                self.s.dtr = True
                self.s.rts = True
        if not self.s.is_open:
            self.s.open()
        self.info = {"port": port}

    def read(self) -> Optional[bytes]:
        n = self.s.in_waiting
        return self.s.read(n or 1)

    def write(self, data: bytes) -> None:
        self.s.write(data)
        self.s.flush()

    def close(self) -> None:
        try:
            self.s.close()
        except Exception:
            pass


class WebReplEnd:
    """A WebREPL connection. Login eats the banner; the stream starts after it."""

    def __init__(self, spec: dict[str, Any]) -> None:
        from . import webrepl

        self.ws = webrepl.WebSocketSerial(spec["url"], spec.get("password"), timeout=0.05)
        self.ws.open()
        self.info = {"url": spec["url"]}

    def read(self) -> Optional[bytes]:
        n = self.ws.inWaiting()
        if not n:
            time.sleep(0.02)
            return b""
        return self.ws.read(n)

    def write(self, data: bytes) -> None:
        self.ws.write(data)

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


class PtyEnd:
    """A program on a POSIX pseudo terminal, with it as its controlling tty.

    The terminal starts raw except for signals: no echo of its own (the
    REPL echoes), no CR->LF on input, and Ctrl-C still raises SIGINT while
    the interpreter is busy, as it would in a person's terminal.
    """

    def __init__(self, spec: dict[str, Any]) -> None:
        import fcntl
        import pty
        import struct
        import termios

        master, slave = pty.openpty()
        attrs = termios.tcgetattr(slave)
        attrs[0] &= ~(termios.ICRNL | termios.IXON | termios.INLCR | termios.IGNCR)
        attrs[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.IEXTEN)
        attrs[3] |= termios.ISIG
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 250, 0, 0))

        def child_setup() -> None:
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        env = dict(os.environ)
        env.setdefault("TERM", "dumb")
        env.update(spec.get("env") or {})
        self.proc = subprocess.Popen(
            spec["argv"],
            cwd=spec.get("cwd") or None,
            env=env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            preexec_fn=child_setup,
            close_fds=True,
        )
        os.close(slave)
        self.fd = master
        self.info = {"pid": self.proc.pid, "argv": spec["argv"]}

    def read(self) -> Optional[bytes]:
        import select

        r, _, _ = select.select([self.fd], [], [], 0.1)
        if not r:
            return None if self.proc.poll() is not None else b""
        try:
            data = os.read(self.fd, 65536)
        except OSError:
            return None  # EIO: the program exited and the tty closed
        return data if data else None

    def write(self, data: bytes) -> None:
        while data:
            n = os.write(self.fd, data)
            data = data[n:]

    def exit_note(self) -> str:
        code = self.proc.poll()
        return f"the program exited with code {code}" if code is not None else "the terminal closed"

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        try:
            os.close(self.fd)
        except OSError:
            pass


def open_endpoint(spec: dict[str, Any]) -> Any:
    kind = spec["kind"]
    if kind == "serial":
        return SerialEnd(spec)
    if kind == "ws":
        return WebReplEnd(spec)
    if kind == "spawn":
        if sys.platform == "win32":
            from .conpty import ConPtyEnd

            return ConPtyEnd(spec)
        return PtyEnd(spec)
    raise ValueError(f"unknown device kind {kind!r}")


def pump_main() -> int:
    """Copy stdin to the device and the device to stdout until stdin closes."""
    inp = sys.stdin.buffer
    out = sys.stdout.buffer
    err = sys.stderr
    header = inp.readline()
    try:
        spec = json.loads(header.decode("utf-8"))
        end = open_endpoint(spec)
    except Exception as e:
        err.write(f"{ERROR} {type(e).__name__}: {e}\n")
        err.flush()
        return 2
    err.write(f"{READY} {json.dumps(end.info)}\n")
    err.flush()
    stop = threading.Event()

    def feed() -> None:
        try:
            while not stop.is_set():
                data = inp.read1(4096)
                if not data:
                    break
                end.write(data)
        except Exception as e:
            err.write(f"{ENDED} writing to the device failed: {e}\n")
            err.flush()
        stop.set()

    threading.Thread(target=feed, daemon=True).start()
    note = "stopped"
    try:
        while not stop.is_set():
            data = end.read()
            if data is None:
                note = end.exit_note() if hasattr(end, "exit_note") else "the device closed"
                break
            if data:
                out.write(data)
                out.flush()
    except Exception as e:
        note = f"reading the device failed: {type(e).__name__}: {e}"
    if not stop.is_set():
        err.write(f"{ENDED} {note}\n")
        err.flush()
    end.close()
    return 0


# ---------------------------------------------------------------------------
# The holder
# ---------------------------------------------------------------------------


class Transcript:
    """One timestamped line per line of output; sends are marked ``>>``."""

    def __init__(self, path: Path) -> None:
        self.f = open(path, "a", encoding="utf-8")  # noqa: SIM115 (open for the holder's life)
        self.at_start = True
        self.lock = threading.Lock()

    @staticmethod
    def _stamp() -> str:
        t = time.time()
        return time.strftime("%H:%M:%S", time.localtime(t)) + ".%03d" % int(t % 1 * 1000)

    def device(self, data: bytes) -> None:
        text = clean(data)
        with self.lock:
            for piece in text.splitlines(keepends=True):
                if self.at_start:
                    self.f.write(self._stamp() + "  ")
                self.f.write(piece)
                self.at_start = piece.endswith("\n")
            self.f.flush()

    def note(self, kind: str, text: str) -> None:
        with self.lock:
            if not self.at_start:
                self.f.write("\n")
                self.at_start = True
            self.f.write(f"{self._stamp()} {kind} {text}\n")
            self.f.flush()


def _describe_sent(data: bytes) -> str:
    names = {3: "Ctrl-C", 1: "Ctrl-A", 2: "Ctrl-B", 4: "Ctrl-D", 5: "Ctrl-E"}
    if len(data) == 1 and data[0] in names:
        return names[data[0]]
    text = data.decode("utf-8", "replace")
    return repr(text) if len(text) <= 400 else repr(text[:400]) + f" (+{len(text) - 400} chars)"


def daemon_main(key: str) -> int:
    d = hold_dir(key)
    spec = json.loads(sys.stdin.readline())
    pump_spec = spec["pump_spec"]
    state: dict[str, Any] = {
        "key": key,
        "device": spec["device"],
        "pid": os.getpid(),
        "state": "starting",
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "pump_python": spec["pump_python"],
    }
    (d / PIDFILE).write_text(str(os.getpid()))
    tr = Transcript(d / TRANSCRIPT)
    tr.note("==", f"holder {os.getpid()} starting for {spec['device']}")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    state["port"] = server.getsockname()[1]
    state["token"] = secrets.token_hex(16)
    _write_json(d / STATE, state)

    env = spec.get("pump_env")
    pump = subprocess.Popen(
        [spec["pump_python"], "-m", "mpftp.hold", "pump"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert pump.stdin and pump.stdout and pump.stderr
    pump.stdin.write((json.dumps(pump_spec) + "\n").encode("utf-8"))
    pump.stdin.flush()
    wlock = threading.Lock()
    done = threading.Event()
    stream = open(d / STREAM, "ab")  # noqa: SIM115 (open for the holder's life)
    written = [0]  # bytes in the stream so far: the offsets clients see

    def set_state(**kw: Any) -> None:
        state.update(kw)
        _write_json(d / STATE, state)

    def read_out() -> None:
        while True:
            data = pump.stdout.read1(65536)
            if not data:
                break
            with wlock:
                stream.write(data)
                stream.flush()
                written[0] += len(data)
            tr.device(data)
        done.set()

    def read_err() -> None:
        for raw in pump.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line.startswith(READY):
                info = json.loads(line[len(READY) :].strip() or "{}")
                set_state(state="ready", endpoint=info)
                tr.note("==", f"ready {json.dumps(info)}")
            elif line.startswith(ERROR):
                msg = line[len(ERROR) :].strip()
                set_state(state="failed", error=msg)
                tr.note("!!", msg)
            elif line.startswith(ENDED):
                msg = line[len(ENDED) :].strip()
                set_state(ended=msg)
                tr.note("!!", msg)
            elif line:
                tr.note("!!", line)

    def handle(conn: socket.socket) -> None:
        with conn:
            conn.settimeout(10)
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
            req = json.loads(buf.split(b"\n", 1)[0])
            if req.get("token") != state["token"]:
                conn.sendall(b'{"ok": false, "error": "bad token"}\n')
                return
            op = req.get("op")
            reply: dict[str, Any] = {"ok": True}
            try:
                if op == "write":
                    data = base64.b64decode(req["data_b64"])
                    with wlock:
                        reply["offset"] = written[0]
                    pump.stdin.write(data)
                    pump.stdin.flush()
                    tr.note(">>", _describe_sent(data))
                elif op == "status":
                    reply["offset"] = written[0]
                    reply["pump_alive"] = pump.poll() is None
                elif op == "stop":
                    reply["stopping"] = True
                    done.set()
                else:
                    reply = {"ok": False, "error": f"unknown op {op!r}"}
            except Exception as e:
                reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))

    def serve() -> None:
        while not done.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    def on_term(*_: Any) -> None:
        done.set()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    for fn in (read_out, read_err, serve):
        threading.Thread(target=fn, daemon=True).start()

    while not done.wait(0.5):
        if pump.poll() is not None:
            break
    # Closing the pump's stdin is its signal to let go of the device.
    try:
        pump.stdin.close()
    except Exception:
        pass
    try:
        pump.wait(5)
    except subprocess.TimeoutExpired:
        pump.terminate()
        try:
            pump.wait(3)
        except subprocess.TimeoutExpired:
            pump.kill()
    time.sleep(0.2)
    final = "failed" if state.get("state") == "failed" else "stopped"
    set_state(state=final, stopped=time.strftime("%Y-%m-%dT%H:%M:%S"), port=None)
    tr.note(
        "==",
        f"holder {os.getpid()} {final}" + (f": {state['ended']}" if state.get("ended") else ""),
    )
    server.close()
    (d / PIDFILE).unlink(missing_ok=True)
    return 0


# ---------------------------------------------------------------------------
# Client side
# ---------------------------------------------------------------------------


def _call(key: str, op: str, **params: Any) -> dict[str, Any]:
    pid = holder_pid(key)
    st = _read_json(hold_dir(key) / STATE)
    if not pid or pid < 0 or not st.get("port"):
        why = f": {st['ended']}" if st.get("ended") else ""
        raise HoldError(f"the holder for {key} is not running{why}")
    req = dict(params, op=op, token=st["token"])
    with socket.create_connection(("127.0.0.1", st["port"]), timeout=10) as s:
        s.sendall((json.dumps(req) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    if not buf.strip():
        raise HoldError(f"the holder for {key} closed the connection without a reply")
    reply = json.loads(buf.split(b"\n", 1)[0])
    if not reply.get("ok"):
        raise HoldError(reply.get("error") or "holder error")
    return reply


def write(key: str, data: bytes) -> int:
    """Send bytes as typed; returns the stream offset at the moment they went."""
    return int(_call(key, "write", data_b64=base64.b64encode(data).decode("ascii"))["offset"])


def get_cursor(key: str) -> int:
    try:
        return int((hold_dir(key) / CURSOR).read_text().strip())
    except Exception:
        return 0


def set_cursor(key: str, offset: int) -> None:
    (hold_dir(key) / CURSOR).write_text(str(offset))


def wait_for(
    key: str,
    since: int,
    done: Callable[[str], Any],
    timeout: float,
    poll: float = 0.03,
) -> tuple[Any, str, int]:
    """Poll the stream from ``since`` until ``done(text)`` is truthy or time runs out.

    Returns (``done``'s value or None, the text, the offset read up to).
    """
    deadline = time.monotonic() + timeout
    while True:
        end = stream_size(key)
        text = clean(read_stream(key, since, end))
        got = done(text)
        if got or time.monotonic() >= deadline:
            return got, text, end
        if holder_pid(key) is None:
            return None, text, end
        time.sleep(poll)


def _busy_message(timeout: float) -> str:
    return (
        f"no prompt yet after {timeout:g} s: the interpreter is still busy (an lv.async_call, "
        "a long call, or a loop that doesn't yield) or waiting for more input. "
        "`mpftp hold read` shows what it prints next; `mpftp hold interrupt` sends Ctrl-C"
    )


def _offset_of(text_len_chars: int, raw: bytes) -> int:
    """Byte offset within ``raw`` of the first ``text_len_chars`` chars of ``clean(raw)``."""
    if text_len_chars <= 0:
        return 0
    seen = 0
    i = 0
    while i < len(raw) and seen < text_len_chars:
        b = raw[i]
        if b == 0x0D:  # stripped by clean()
            i += 1
            continue
        # One char: an ASCII byte, or a UTF-8 lead byte and its continuations.
        j = i + 1
        while j < len(raw) and (raw[j] & 0xC0) == 0x80:
            j += 1
        i = j
        seen += 1
    return i


_COMPOUND = re.compile(r"\s*(for|while|if|def|class|with|try|async|@)\b|.*:\s*$")


def ask(key: str, line: str, timeout: float = 10.0, marker: Optional[str] = None) -> dict[str, Any]:
    """Type ``line`` (paste mode if it has newlines) and wait for the prompt after it."""
    code = line.replace("\r\n", "\n")
    paste = "\n" in code.rstrip("\n")
    t0 = time.monotonic()
    if paste:
        since = _paste(key, code, timeout)
        echo, after = None, PASTE_PROMPT
    else:
        code = code.rstrip("\n")
        # A one-line block (``for i in x: f(i)``) ends at an empty line.
        enter = b"\r\r" if _COMPOUND.match(code) else b"\r"
        since = write(key, code.encode("utf-8") + enter)
        echo, after = code, None
    still = {"text": None, "at": 0.0}

    def done(text: str) -> Any:
        r = parse_reply(text, echo, after)
        if r["prompt"]:
            return r
        # Waiting for more of a block: say so once it has sat still a moment.
        if text != still["text"]:
            still["text"], still["at"] = text, time.monotonic()
        elif r.get("continuation") and time.monotonic() - still["at"] > 0.5:
            return {"continuation_only": True}
        return None

    got, text, end = wait_for(key, since, done, timeout)
    elapsed = int((time.monotonic() - t0) * 1000)
    if got and got.get("prompt"):
        raw = read_stream(key, since, end)
        nxt = since + _offset_of(got["consumed"], raw)
        set_cursor(key, nxt)
        result: dict[str, Any] = {
            "ok": True,
            "prompt": True,
            "output": got["output"],
            "elapsed_ms": elapsed,
            "next": nxt,
        }
        if marker:
            result["marked"] = marked(got["output"], marker)
        return result
    r = parse_reply(text, echo, after)
    result = {
        "ok": False,
        "prompt": False,
        "error": _busy_message(timeout),
        "output": r["output"],
        "elapsed_ms": elapsed,
        "next": since,
    }
    if r.get("continuation"):
        result["error"] = (
            "the REPL is waiting for the rest of a block (`... `): finish it with "
            "`mpftp hold ask ''`, or send the whole block at once (it goes in paste mode)"
        )
    if holder_pid(key) is None:
        st = _read_json(hold_dir(key) / STATE)
        result["error"] = "the holder stopped" + (f": {st['ended']}" if st.get("ended") else "")
    return result


def _paste(key: str, code: str, timeout: float) -> int:
    """Friendly-REPL paste mode (Ctrl-E ... Ctrl-D), one line at a time.

    Each line waits for the device to echo it, so a board with a small input
    buffer isn't overrun. Returns the offset at the Ctrl-E.
    """
    since = write(key, b"\x05")
    got, _, _ = wait_for(key, since, lambda t: PASTE_PROMPT in t, min(timeout, 5.0))
    if not got:
        raise HoldError("the REPL did not enter paste mode (Ctrl-E); is it at a `>>>` prompt?")
    for ln in code.rstrip("\n").split("\n"):
        before = stream_size(key)
        write(key, ln.encode("utf-8") + b"\r")
        wait_for(key, before, lambda t: "\n" in t, 2.0, poll=0.01)
    write(key, b"\x04")
    return since


def interrupt(key: str, timeout: float = 5.0) -> dict[str, Any]:
    """Ctrl-C, then wait for the friendly prompt.

    If ``hold exec`` left the raw REPL entered (it timed out), Ctrl-C ends
    the code and Ctrl-B goes back to the friendly REPL.
    """
    raw_pending = (hold_dir(key) / RAW_FLAG).exists()
    since = write(key, b"\x03")
    if raw_pending:
        # The raw REPL answers Ctrl-C with nothing at its own prompt, and with
        # a traceback inside code: give it a moment either way.
        wait_for(key, since, lambda t: t.rstrip().endswith(">"), min(timeout, 1.5))
        write(key, b"\x02")
        (hold_dir(key) / RAW_FLAG).unlink(missing_ok=True)
    got, text, end = wait_for(key, since, lambda t: _line_start_prompt(t, 0) >= 0, timeout)
    if got:
        p = text.rfind(PROMPT)
        nxt = since + _offset_of(p + len(PROMPT), read_stream(key, since, end))
        set_cursor(key, nxt)
        return {"ok": True, "prompt": True, "output": text[:p], "next": nxt}
    return {
        "ok": False,
        "prompt": False,
        "error": f"no prompt within {timeout:g} s of Ctrl-C (over WebREPL a loop that never "
        "yields can't be interrupted without micropython-pydevices' patch 0011; use serial or reset)",
        "output": text,
        "next": end,
    }


def raw_exec(key: str, code: str, timeout: float = 30.0) -> dict[str, Any]:
    """Run ``code`` through the raw REPL (Ctrl-A), then go back to the friendly one.

    This is the one hold command that leaves the friendly REPL, and only
    because you asked. Good for a block too long to type, and for code whose
    output must not be mixed with an echo.
    """
    endpoint = _read_json(hold_dir(key) / STATE).get("endpoint") or {}
    if endpoint.get("console") == "conpty":
        raise HoldError(
            "a Windows pseudo console drops the raw REPL's Ctrl-D framing, so `hold exec` "
            "can't tell where a result ends there; use `hold ask` (multi-line code goes in paste mode)"
        )
    t0 = time.monotonic()
    since = write(key, b"\x01")
    got, text, _ = wait_for(key, since, lambda t: RAW_BANNER in t, min(timeout, 5.0))
    if not got:
        raise HoldError(
            "the REPL did not enter the raw REPL (Ctrl-A); it is busy or not at a prompt. "
            "`mpftp hold interrupt` first if it is busy"
        )
    (hold_dir(key) / RAW_FLAG).write_text("1")
    start = stream_size(key)
    data = code.encode("utf-8")
    for i in range(0, len(data), 256):
        write(key, data[i : i + 256])
        time.sleep(0.01)
    write(key, b"\x04")
    got, text, end = wait_for(key, start, lambda t: parse_raw_reply(t), timeout)
    elapsed = int((time.monotonic() - t0) * 1000)
    if not got:
        return {
            "ok": False,
            "error": f"no result after {timeout:g} s; the code is still running in the raw REPL. "
            "`mpftp hold interrupt` stops it and goes back to the friendly REPL",
            "output": text,
            "elapsed_ms": elapsed,
        }
    back = write(key, b"\x02")
    ok, _, end = wait_for(key, back, lambda t: _line_start_prompt(t, 0) >= 0, 5.0)
    (hold_dir(key) / RAW_FLAG).unlink(missing_ok=True)
    set_cursor(key, end)
    result = {"ok": not got["error"], "output": got["output"], "elapsed_ms": elapsed, "next": end}
    if got["error"]:
        result["error"] = got["error"]
    if not ok:
        result["warning"] = "no friendly prompt after Ctrl-B"
    return result


def read(
    key: str,
    since: Optional[int] = None,
    wait: float = 0.0,
    until: Optional[str] = None,
    limit: int = 65536,
) -> dict[str, Any]:
    start = get_cursor(key) if since is None else since
    size = stream_size(key)
    if start > size:
        start = size
    if until:
        rx = re.compile(until, re.M)
        got, text, end = wait_for(key, start, lambda t: rx.search(t), wait, poll=0.05)
        if got:
            # Stop just after the line that matched.
            nl = text.find("\n", got.end())
            cut = len(text) if nl < 0 else nl + 1
            end = start + _offset_of(cut, read_stream(key, start, end))
            text = text[:cut]
        matched = bool(got)
    else:
        if wait and size == start:
            wait_for(key, start, lambda t: t, wait, poll=0.05)
        end = stream_size(key)
        text = clean(read_stream(key, start, end))
        matched = None
    skipped = 0
    if len(text) > limit:
        skipped = len(text) - limit
        text = text[-limit:]
    set_cursor(key, end)
    result: dict[str, Any] = {"ok": True, "from": start, "next": end, "text": text}
    if skipped:
        result["skipped_chars"] = skipped
    if matched is not None:
        result["matched"] = matched
    return result


def _wslpath_w(path: str) -> str:
    r = subprocess.run(["wslpath", "-w", path], capture_output=True, text=True, timeout=5)
    if r.returncode != 0 or not r.stdout.strip():
        raise HoldError(f"wslpath could not translate {path}: {r.stderr.strip()}")
    return r.stdout.strip()


def _on_wsl() -> bool:
    return bool(os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"))


def windows_spawn_spec(argv: list[str], cwd: Optional[str], env: dict[str, str]) -> dict[str, Any]:
    """Translate a ``.exe`` command from WSL terms into the paths Windows needs.

    The program and the working directory become Windows paths (a WSL symlink
    won't open from Windows, so it is resolved first). ``MICROPYPATH`` goes
    over as a ``;`` list of Windows paths, always, because a Windows
    MicroPython without it silently loads ``%USERPROFILE%\\.micropython\\lib``.
    """
    if not _on_wsl():
        return {"argv": argv, "cwd": cwd, "env": env}
    exe = argv[0]
    if "/" not in exe:
        found = subprocess.run(["which", exe], capture_output=True, text=True).stdout.strip()
        exe = found or exe
    if exe.startswith("/"):
        exe = _wslpath_w(os.path.realpath(exe))
    out_env = dict(env)
    mpp = out_env.get("MICROPYPATH", os.environ.get("MICROPYPATH"))
    if mpp is not None:
        parts = []
        for p in mpp.split(":"):
            if p.startswith("/"):
                parts.append(_wslpath_w(os.path.realpath(p)))
            elif p:
                parts.append(p)
        out_env["MICROPYPATH"] = ";".join(parts)
    wcwd = _wslpath_w(os.path.realpath(cwd or os.getcwd()))
    return {"argv": [exe, *argv[1:]], "cwd": wcwd, "env": out_env}


def start(
    device: Optional[str] = None,
    spawn: Optional[str] = None,
    cwd: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    name: Optional[str] = None,
    baud: int = 115200,
    password: Optional[str] = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    from . import cli, webrepl

    if bool(device) == bool(spawn):
        raise HoldError("give a device (-d COM42, -d ws://HOST) or --spawn 'COMMAND', not both")
    env = dict(env or {})
    if spawn:
        argv = shlex.split(spawn)
        if not argv:
            raise HoldError("--spawn needs a command")
        label = spawn
        key = key_for(name) if name else spawn_name(argv)
        if is_windows_exe(argv[0]):
            if not _on_wsl() and sys.platform != "win32":
                raise HoldError(f"{argv[0]} is a Windows program; run it from WSL or Windows")
            pump_spec = dict(kind="spawn", **windows_spawn_spec(argv, cwd, env))
            pump_python = cli.resolve_python() if _on_wsl() else sys.executable
        else:
            if sys.platform == "win32":
                raise HoldError(
                    "a Windows host can --spawn only a .exe (it runs in a pseudo console)"
                )
            pump_spec = {"kind": "spawn", "argv": argv, "cwd": cwd, "env": env}
            pump_python = sys.executable
    else:
        assert device is not None
        label = device
        key = key_for(name) if name else key_for(device)
        if webrepl.is_network_device(device):
            pump_spec = {"kind": "ws", "url": device, "password": password}
            pump_python = cli.resolve_python() if _on_wsl() else sys.executable
        elif device.lower().startswith("ble://"):
            raise HoldError("a ble:// board can't be held yet; use serial or ws://")
        else:
            pump_spec = {"kind": "serial", "port": device, "baud": baud}
            is_com = bool(re.fullmatch(r"(?i)com\d+", device))
            pump_python = cli.resolve_python() if (_on_wsl() and is_com) else sys.executable

    d = hold_dir(key)
    d.mkdir(parents=True, exist_ok=True)
    pid = holder_pid(key)
    if pid:
        st = _read_json(d / STATE)
        who = f"pid {pid}" if pid > 0 else "a start in progress"
        raise HoldError(
            f"{label} is already held ({who}, since {st.get('started', '?')}); "
            f"one holder per board. `mpftp hold stop -d {key}` lets it go"
        )
    try:
        fd = os.open(str(d / PIDFILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise HoldError(f"{label} is being started by someone else right now") from None
    with os.fdopen(fd, "w") as f:
        f.write("starting")
    # A fresh log for a fresh hold; the transcript keeps its history.
    for name_ in (STREAM, CURSOR, RAW_FLAG, STATE):
        (d / name_).unlink(missing_ok=True)

    # The holder and its pump import this same package, from wherever it is.
    src = str(Path(__file__).resolve().parents[1])
    pp = os.environ.get("PYTHONPATH", "")
    if src not in pp.split(os.pathsep):
        os.environ["PYTHONPATH"] = src + (os.pathsep + pp if pp else "")
    env_self = dict(os.environ)
    # A Windows pump from WSL gets PYTHONPATH (and a live WSL_INTEROP) the
    # way the sidecar does.
    pump_env = (cli._wslenv_forwarded_env(pump_python) if _on_wsl() else None) or env_self
    payload = {
        "device": label,
        "pump_python": pump_python,
        "pump_spec": pump_spec,
        "pump_env": pump_env,
    }
    log = open(d / "holder.err", "ab")  # noqa: SIM115 (handed to the holder, closed below)
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [sys.executable, "-m", "mpftp.hold", "daemon", key],
        stdin=subprocess.PIPE,
        stdout=log,
        stderr=log,
        env=env_self,
        close_fds=True,
        **kwargs,
    )
    assert proc.stdin
    proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
    proc.stdin.close()
    log.close()

    deadline = time.monotonic() + timeout
    st: dict[str, Any] = {}
    while time.monotonic() < deadline:
        st = _read_json(d / STATE)
        if st.get("state") in ("ready", "failed", "stopped"):
            break
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    if st.get("state") != "ready":
        err = st.get("error") or st.get("ended")
        if not err:
            try:
                err = (d / "holder.err").read_text(errors="replace").strip().splitlines()[-1]
            except Exception:
                err = "it did not report ready within %g s" % timeout
        if proc.poll() is None:
            try:
                os.kill(proc.pid, signal.SIGTERM)
            except Exception:
                pass
        else:
            (d / PIDFILE).unlink(missing_ok=True)
        raise HoldError(f"could not hold {label}: {err}")
    result: dict[str, Any] = {
        "ok": True,
        "key": key,
        "device": label,
        "pid": st["pid"],
        "endpoint": st.get("endpoint"),
        "log": str(d / TRANSCRIPT),
        "stream": str(d / STREAM),
    }
    if spawn:
        # A fresh interpreter prints its banner and a prompt; wait for it.
        got, text, end = wait_for(key, 0, lambda t: _line_start_prompt(t, 0) >= 0, 15.0)
        result["output"] = text
        result["prompt"] = bool(got)
        set_cursor(key, end)
    return result


def stop(key: str, timeout: float = 8.0) -> dict[str, Any]:
    pid = holder_pid(key)
    if not pid or pid < 0:
        return {"ok": True, "key": key, "was_running": False}
    how = "asked"
    try:
        _call(key, "stop")
    except Exception:
        how = "SIGTERM"
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and pid_alive(pid):
        time.sleep(0.1)
    if pid_alive(pid):
        how = "SIGKILL"
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        time.sleep(0.3)
    (hold_dir(key) / PIDFILE).unlink(missing_ok=True)
    st = _read_json(hold_dir(key) / STATE)
    return {
        "ok": True,
        "key": key,
        "was_running": True,
        "pid": pid,
        "how": how,
        "state": st.get("state"),
    }


def status(key: Optional[str] = None) -> Any:
    if key is None:
        return list_holds()
    d = hold_dir(key)
    st = _read_json(d / STATE)
    st.pop("token", None)
    pid = holder_pid(key)
    st["live"] = bool(pid and pid > 0)
    st["offset"] = stream_size(key)
    st["cursor"] = get_cursor(key)
    st["raw_pending"] = (d / RAW_FLAG).exists()
    st["log"] = str(d / TRANSCRIPT)
    return st


def main(argv: Optional[list[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["pump"]:
        return pump_main()
    if args[:1] == ["daemon"] and len(args) == 2:
        return daemon_main(args[1])
    print(
        "usage: python -m mpftp.hold pump | daemon KEY (started by `mpftp hold start`)",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
