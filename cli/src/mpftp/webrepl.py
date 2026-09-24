"""Reach a board over Wi-Fi through MicroPython's WebREPL.

A WebREPL session is a WebSocket whose text frames carry the REPL's byte
stream (``extmod/modwebrepl.c`` hands them to ``os.dupterm``). That is the
same stream a USB serial port carries, so :class:`WebSocketSerial` dresses the
socket up as a pyserial port and :func:`open_transport` hands it to mpremote's
``SerialTransport``. Raw REPL, raw-paste, exec and every ``fs_*`` operation
then work unchanged. The design note is ``docs/plans/wifi-webrepl.md``.

Only the standard library is used: the sidecar runs on whatever Python the
user has, including Windows Python vendored into the VS Code extension.
"""

from __future__ import annotations

import base64
import hashlib
import os
import select
import socket
import struct
import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

DEFAULT_PORT = 8266

#: ``modwebrepl.c`` keeps the password in ``char webrepl_passwd[10]``.
MAX_PASSWORD_LEN = 9

_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

#: The board's parser rejects the 8-byte length form, so a frame must stay
#: under 64 KiB. 1 KiB keeps each frame inside one lwIP segment or two.
MAX_FRAME = 1024

_PASSWORD_PROMPT = b"Password: "
_CONNECTED = b"WebREPL connected"
_DENIED = b"Access denied"

#: With pyserial's ``timeout=None`` a read blocks until the bytes arrive. A
#: dead serial port errors, but a half-open TCP connection just goes quiet, so
#: blocking reads give up after this long instead of hanging the sidecar.
STALL_TIMEOUT = 20.0


class WebReplError(OSError):
    """The WebREPL connection failed or dropped.

    The message says "port is closed" when the socket is gone, so the
    sidecar's dead-handle recovery (``is_dead_serial_error``) treats a dropped
    connection the way it treats a yanked USB cable.
    """


class WebReplAuthError(RuntimeError):
    """The board rejected the password. Never retried."""


def is_network_device(device: Optional[str]) -> bool:
    return bool(device) and device.strip().lower().startswith(("ws://", "wss://"))


def parse_device(device: str) -> tuple[str, int, str]:
    """``ws://host[:port][/path]`` -> ``(host, port, path)``."""
    parts = urlsplit(device.strip())
    if parts.scheme.lower() == "wss":
        raise ValueError(
            f"{device}: MicroPython's WebREPL has no TLS; use ws:// on a network you trust"
        )
    if parts.scheme.lower() != "ws" or not parts.hostname:
        raise ValueError(f"{device}: expected ws://HOST[:PORT], e.g. ws://192.168.1.50:8266")
    if parts.username or parts.password:
        raise ValueError(
            f"{device}: don't put the password in the address; set MPFTP_WEBREPL_PASSWORD "
            "or webreplPassword in ~/.mpftp/config.json"
        )
    return parts.hostname, parts.port or DEFAULT_PORT, parts.path or "/"


def check_password(password: Optional[str], device: str) -> str:
    if not password:
        raise WebReplAuthError(
            f"{device}: no WebREPL password. Set MPFTP_WEBREPL_PASSWORD, or "
            "webreplPassword in ~/.mpftp/config.json"
        )
    if len(password) > MAX_PASSWORD_LEN or "\r" in password or "\n" in password:
        raise WebReplAuthError(
            f"{device}: a WebREPL password is 1 to {MAX_PASSWORD_LEN} characters "
            "with no line breaks (the board keeps only 9)"
        )
    return password


def host_answers_ping(host: str, timeout: float = 1.0) -> Optional[bool]:
    """Does ``host`` answer one ICMP echo? None when ``ping`` can't be run.

    A board spinning in a loop that never yields still answers ping, because
    lwIP runs in its own task. That is how a busy board is told apart from one
    that is switched off or gone from the network.
    """
    import subprocess
    import sys

    if sys.platform == "win32":
        cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), host]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(round(timeout)))), host]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # Windows counts "Destination host unreachable" as a reply; a real echo
    # reply carries a TTL on every platform.
    return "ttl=" in (proc.stdout or "").lower()


def encode_frame(opcode: int, payload: bytes, mask_key: Optional[bytes] = None) -> bytes:
    """One final, client-masked frame (RFC 6455 section 5.2)."""
    if mask_key is None:
        mask_key = os.urandom(4)
    n = len(payload)
    if n < 126:
        header = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
    elif n < 0x10000:
        header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
    else:
        raise ValueError("frame too large for the board's WebSocket parser")
    masked = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))
    return header + mask_key + masked


class FrameParser:
    """Incremental decoder for frames the board sends (unmasked, maybe masked)."""

    def __init__(self) -> None:
        self._buf = bytearray()

    @property
    def pending(self) -> int:
        """Bytes of a frame that has started arriving but isn't complete."""
        return len(self._buf)

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self._buf += data
        frames: list[tuple[int, bytes]] = []
        while True:
            b = self._buf
            if len(b) < 2:
                break
            opcode = b[0] & 0x0F
            masked = b[1] & 0x80
            n = b[1] & 0x7F
            pos = 2
            if n == 126:
                if len(b) < 4:
                    break
                n = struct.unpack_from("!H", b, 2)[0]
                pos = 4
            elif n == 127:
                if len(b) < 10:
                    break
                n = struct.unpack_from("!Q", b, 2)[0]
                pos = 10
            key = b""
            if masked:
                if len(b) < pos + 4:
                    break
                key = bytes(b[pos : pos + 4])
                pos += 4
            if len(b) < pos + n:
                break
            payload = bytes(b[pos : pos + n])
            if key:
                payload = bytes(x ^ key[i & 3] for i, x in enumerate(payload))
            del b[: pos + n]
            frames.append((opcode, payload))
        return frames


def _accept_key(key: bytes) -> bytes:
    return base64.b64encode(hashlib.sha1(key + _WS_GUID).digest())


class WebSocketSerial:
    """The pyserial surface mpremote's ``SerialTransport`` uses, over WebREPL.

    ``open()`` does the HTTP upgrade and the password login, so a wrong
    password or a board that isn't listening fails there, with a message, in
    seconds. After that ``read``/``write``/``inWaiting`` move REPL bytes.
    Incoming frames are decoded when a caller asks for data; there is no
    background thread. A lock makes that safe for the sidecar's REPL reader
    running beside a command.
    """

    def __init__(
        self,
        device: str,
        password: Optional[str],
        *,
        timeout: Optional[float] = None,
        connect_timeout: float = 5.0,
        login_timeout: float = 5.0,
        create_connection: Callable[..., Any] = socket.create_connection,
    ) -> None:
        self.port = device
        self.host, self.tcp_port, self.path = parse_device(device)
        self._password = check_password(password, device)
        self.timeout = timeout
        self.write_timeout: Optional[float] = None
        self.connect_timeout = connect_timeout
        self.login_timeout = login_timeout
        self._create_connection = create_connection
        self._sock: Any = None
        self._parser = FrameParser()
        self._rx = bytearray()  # text frames: the REPL stream
        self._brx = bytearray()  # binary frames: file-transfer replies
        self._last_data_op = OP_TEXT
        self._closed_reason: Optional[str] = None
        #: REPL bytes received so far, counted before any reader takes them, so
        #: a caller can tell whether the board said anything since a moment.
        self.rx_total = 0
        self._lock = threading.RLock()
        self._wlock = threading.Lock()
        # Accepted and ignored: a network link has no modem lines.
        self.dtr = True
        self.rts = True

    # --- pyserial compatibility -------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._sock is not None and self._closed_reason is None

    def isOpen(self) -> bool:  # noqa: N802 (pyserial spelling)
        return self.is_open

    def setDTR(self, value: bool = True) -> None:  # noqa: N802
        self.dtr = value

    def setRTS(self, value: bool = True) -> None:  # noqa: N802
        self.rts = value

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        with self._lock:
            self._pump(0)
            self._rx.clear()

    flushInput = reset_input_buffer  # noqa: N815

    @property
    def in_waiting(self) -> int:
        return self.inWaiting()

    def inWaiting(self) -> int:  # noqa: N802
        with self._lock:
            # Always read the socket, even with bytes already waiting: callers
            # watch ``rx_total`` for the board's answer, and bytes left over
            # from an earlier reply must not hide it.
            self._pump(0)
            if not self._rx and self._closed_reason:
                raise WebReplError(self._closed_message())
            return len(self._rx)

    def read(self, size: int = 1) -> bytes:
        limit = STALL_TIMEOUT if self.timeout is None else self.timeout
        deadline = time.monotonic() + limit
        with self._lock:
            while len(self._rx) < size:
                if self._closed_reason:
                    if self._rx:
                        break
                    raise WebReplError(self._closed_message())
                left = deadline - time.monotonic()
                if left <= 0:
                    if self.timeout is None:
                        raise WebReplError(
                            f"{self.port}: no data for {STALL_TIMEOUT:.0f} s; "
                            "port is closed (WebREPL connection stalled)"
                        )
                    break
                self._pump(min(left, 0.2))
            out = bytes(self._rx[:size])
            del self._rx[:size]
            return out

    def read_binary(self, size: int, timeout: float = STALL_TIMEOUT) -> bytes:
        """Exactly ``size`` bytes of binary-frame payload (WebREPL file replies)."""
        deadline = time.monotonic() + timeout
        with self._lock:
            while len(self._brx) < size:
                if self._closed_reason:
                    raise WebReplError(self._closed_message())
                left = deadline - time.monotonic()
                if left <= 0:
                    raise WebReplError(
                        f"{self.port}: file transfer stalled for {timeout:.0f} s; "
                        "port is closed (no reply from the board)"
                    )
                self._pump(min(left, 0.2))
            out = bytes(self._brx[:size])
            del self._brx[:size]
            return out

    def write_binary(self, data: bytes) -> None:
        """Send ``data`` as binary frames (WebREPL's file-transfer channel)."""
        data = bytes(data)
        if self._closed_reason or self._sock is None:
            raise WebReplError(self._closed_message())
        for i in range(0, max(len(data), 1), MAX_FRAME):
            self._send_frame(OP_BINARY, data[i : i + MAX_FRAME])

    def write(self, data: bytes) -> int:
        data = bytes(data)
        if self._closed_reason or self._sock is None:
            raise WebReplError(self._closed_message())
        for i in range(0, len(data), MAX_FRAME):
            self._send_frame(OP_TEXT, data[i : i + MAX_FRAME])
        return len(data)

    def close(self) -> None:
        sock = self._sock
        if sock is None:
            return
        if not self._closed_reason:
            try:
                self._send_frame(OP_CLOSE, struct.pack("!H", 1000))
            except Exception:
                pass
        self._closed_reason = self._closed_reason or "closed by mpftp"
        try:
            sock.close()
        except Exception:
            pass
        self._sock = None

    # --- connection -------------------------------------------------------

    def open(self) -> None:
        where = f"{self.host}:{self.tcp_port}"
        try:
            self._sock = self._create_connection(
                (self._resolve_host(), self.tcp_port), timeout=self.connect_timeout
            )
        except socket.timeout as e:
            raise WebReplError(
                f"{self.port}: no answer from {where} in {self.connect_timeout:.0f} s. "
                "Is the board on this network at that address? A program that never "
                "sleeps or waits also keeps WebREPL from answering."
            ) from e
        except ConnectionRefusedError as e:
            raise WebReplError(
                f"{self.port}: {where} refused the connection. The board is up but "
                "WebREPL isn't listening; run webrepl.start() on it."
            ) from e
        except OSError as e:
            raise WebReplError(f"{self.port}: cannot reach {where}: {e}") from e
        try:
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        try:
            self._upgrade()
            self._login()
        except BaseException:
            self._closed_reason = self._closed_reason or "open failed"
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
            raise

    def _resolve_host(self) -> str:
        """The address to dial. A ``.local`` name the OS can't resolve (Linux
        without nss-mdns) gets one mDNS query of our own."""
        host = self.host
        if not host.lower().endswith(".local"):
            return host
        try:
            socket.getaddrinfo(host, self.tcp_port, socket.AF_INET)
            return host
        except (OSError, UnicodeError):
            pass
        from . import mdns

        return mdns.query(host) or host

    def _upgrade(self) -> None:
        key = base64.b64encode(os.urandom(16))
        # modwebrepl's handshake matches these header names exactly.
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.tcp_port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key.decode('ascii')}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        self._sock.settimeout(self.login_timeout)
        try:
            self._sock.sendall(request)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = self._sock.recv(1024)
                if not chunk:
                    break
                head += chunk
                if len(head) > 8192:
                    break
        except socket.timeout as e:
            raise WebReplError(
                f"{self.port}: the board accepted the connection but never answered "
                "the WebSocket upgrade"
            ) from e
        head, sep, rest = head.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if not sep or b" 101 " not in status + b" ":
            raise WebReplError(
                f"{self.port}: not a WebREPL server (answered {status[:60]!r})"
            )
        accept = None
        for line in head.split(b"\r\n")[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"sec-websocket-accept":
                accept = value.strip()
        if accept != _accept_key(key):
            raise WebReplError(f"{self.port}: WebSocket upgrade answered with the wrong key")
        self._sock.settimeout(None)
        self._sock.setblocking(False)
        if rest:
            self._absorb(rest)

    def _login(self) -> None:
        text = self._read_until_any((_PASSWORD_PROMPT,), self.login_timeout, "the password prompt")
        del text
        self.write(self._password.encode("utf-8") + b"\r")
        seen = self._read_until_any((_CONNECTED, _DENIED), self.login_timeout, "the login reply")
        if _DENIED in seen:
            self._closed_reason = "password rejected"
            raise WebReplAuthError(
                f"{self.port}: the board rejected the WebREPL password "
                "(check MPFTP_WEBREPL_PASSWORD / webreplPassword)"
            )
        # The banner ends in the friendly prompt, sent in the same write.
        self._read_until_any((b">>> ",), self.login_timeout, "the prompt after login")

    def _read_until_any(self, needles: tuple[bytes, ...], limit: float, what: str) -> bytes:
        deadline = time.monotonic() + limit
        with self._lock:
            while True:
                data = bytes(self._rx)
                for n in needles:
                    at = data.find(n)
                    if at >= 0:
                        del self._rx[: at + len(n)]
                        return data[: at + len(n)]
                if self._closed_reason:
                    if not data:
                        raise WebReplError(
                            f"{self.port}: the board closed the connection before {what}. "
                            "Another WebREPL client may be connected (the board allows one)."
                        )
                    raise WebReplError(
                        f"{self.port}: the board closed the connection before {what} "
                        f"(it said {data[-80:]!r})"
                    )
                left = deadline - time.monotonic()
                if left <= 0:
                    raise WebReplError(
                        f"{self.port}: no {what} within {limit:.0f} s (got {data[-80:]!r})"
                    )
                self._pump(min(left, 0.2))

    # --- frames -----------------------------------------------------------

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        frame = encode_frame(opcode, payload)
        limit = self.write_timeout
        deadline = None if limit is None else time.monotonic() + limit
        with self._wlock:
            view = memoryview(frame)
            while view:
                sock = self._sock
                if sock is None:
                    raise WebReplError(self._closed_message())
                try:
                    sent = sock.send(view)
                except (BlockingIOError, InterruptedError):
                    sent = 0
                except OSError as e:
                    self._closed_reason = f"send failed: {e}"
                    raise WebReplError(self._closed_message()) from e
                view = view[sent:]
                if view:
                    wait = 0.5
                    if deadline is not None:
                        wait = deadline - time.monotonic()
                        if wait <= 0:
                            raise WebReplError(
                                f"{self.port}: write timeout; port is closed "
                                "(the board stopped reading)"
                            )
                    select.select([], [sock], [], min(wait, 0.5))

    def _pump(self, wait: float) -> None:
        """Receive whatever has arrived (waiting up to ``wait`` s for the first byte)."""
        sock = self._sock
        if sock is None or self._closed_reason:
            return
        try:
            ready, _, _ = select.select([sock], [], [], max(0.0, wait))
        except (OSError, ValueError) as e:
            self._closed_reason = f"socket error: {e}"
            return
        if not ready:
            return
        while True:
            try:
                chunk = sock.recv(65536)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as e:
                self._closed_reason = f"connection lost: {e}"
                return
            if not chunk:
                self._closed_reason = "the board closed the connection"
                return
            self._absorb(chunk)

    def _absorb(self, data: bytes) -> None:
        for opcode, payload in self._parser.feed(data):
            if opcode == OP_CONT:
                opcode = self._last_data_op
            if opcode == OP_TEXT:
                self._last_data_op = opcode
                self._rx += payload
                self.rx_total += len(payload)
            elif opcode == OP_BINARY:
                self._last_data_op = opcode
                self._brx += payload
            elif opcode == OP_PING:
                try:
                    self._send_frame(OP_PONG, payload)
                except Exception:
                    pass
            elif opcode == OP_CLOSE:
                self._closed_reason = "the board closed the connection"
        if self._parser.pending and not self._closed_reason:
            # The board writes a frame's header and payload as two socket
            # writes, and lwIP's Nagle holds the payload until the header is
            # ACKed, which the host delays by up to 200 ms. Sending anything
            # carries the ACK at once; an empty pong is ignored by the board.
            try:
                self._send_frame(OP_PONG, b"")
            except Exception:
                pass

    def _closed_message(self) -> str:
        reason = self._closed_reason or "not open"
        return f"{self.port}: port is closed ({reason})"


#: WebREPL's file-transfer header (``struct webrepl_file`` in modwebrepl.c).
_FILE_HDR = struct.Struct("<2sBBQLH64s")
_PUT_FILE = 1
#: ``fname`` is 64 bytes and must end in a NUL.
MAX_PUT_NAME = 63
#: Board-side read size when streaming a file back.
GET_CHUNK = 4096
#: ``MP_STREAM_SET_DATA_OPTS`` and ``FRAME_BIN`` from py/stream.h, modwebsocket.h.
_SET_DATA_OPTS = 9
_FRAME_BIN = 2

_TRANSPORT_CLASS: Any = None


def _transport_class() -> Any:
    """Build the ``SerialTransport`` subclass once mpremote is importable."""
    global _TRANSPORT_CLASS
    if _TRANSPORT_CLASS is not None:
        return _TRANSPORT_CLASS

    from mpremote.transport import TransportError, TransportExecError, _convert_filesystem_error
    from mpremote.transport_serial import SerialTransport

    class WebReplTransport(SerialTransport):
        """mpremote's serial transport with a WebREPL socket as its port.

        Three things differ from serial, each for a measured reason (see
        docs/plans/wifi-webrepl.md):

        - Commands go in plain raw REPL, written in one piece. Raw-paste's
          128-byte window costs a Wi-Fi round trip per window; TCP already
          does the flow control raw-paste exists to provide.
        - Uploads use WebREPL's binary PUT, which the board reads 512 bytes at
          a time, instead of ``f.write(b'...')`` execs it parses byte by byte.
        - Downloads are streamed by the board as binary WebSocket frames
          instead of printed, because printed output is also mirrored to the
          board's UART console and runs at its speed.
        """

        def __init__(self, ws: WebSocketSerial) -> None:  # pyserial is not used
            self.in_raw_repl = False
            self.use_raw_paste = False
            self.device_name = ws.port
            self.mounted = False
            self.serial = ws
            self._stream_ok: Optional[bool] = None

        def exec_raw_no_follow(self, command: Any) -> None:
            command_bytes = command if isinstance(command, bytes) else command.encode("utf8")
            data = self.read_until(1, b">")
            if not data.endswith(b">"):
                raise TransportError("could not enter raw repl")
            self.serial.write(command_bytes + b"\x04")
            data = self.serial.read(2)
            if data != b"OK":
                raise TransportError("could not exec command (response: %r)" % data)

        # --- files ---------------------------------------------------------

        def fs_writefile(self, dest, data, chunk_size=256, progress_callback=None):
            name = dest.encode("utf-8")
            if len(name) > MAX_PUT_NAME or not self.in_raw_repl:
                return super().fs_writefile(dest, data, chunk_size, progress_callback)
            data = bytes(data)
            # A PUT the board can't open raises inside its REPL input path,
            # which ends the WebREPL session. Open (and truncate) it here first
            # so a bad path is an ordinary OSError.
            try:
                self.exec("open(%r,'wb').close()" % dest)
            except TransportExecError as e:
                raise _convert_filesystem_error(e, dest) from None
            ws = self.serial
            ws.write_binary(_FILE_HDR.pack(b"WA", _PUT_FILE, 0, 0, len(data), len(name), name))
            self._file_reply(dest, "open")
            step = 16 * 1024
            for i in range(0, len(data), step):
                ws.write_binary(data[i : i + step])
                if progress_callback:
                    progress_callback(min(i + step, len(data)), len(data))
            self._file_reply(dest, "write")
            # modwebrepl ignores a failed flash write (assert(0) is compiled
            # out), so check what landed.
            size = int(self.eval("__import__('os').stat(%r)[6]" % dest))
            if size != len(data):
                raise TransportError(
                    f"{dest}: board holds {size} of {len(data)} bytes after upload "
                    "(filesystem full?)"
                )

        def _file_reply(self, path: str, what: str) -> None:
            reply = self.serial.read_binary(4)
            if reply[:2] != b"WB" or reply[2:] != b"\x00\x00":
                raise TransportError(f"{path}: WebREPL {what} failed (reply {reply!r})")

        def fs_readfile(self, src, chunk_size=256, progress_callback=None):
            if not self.in_raw_repl or not self._can_stream():
                return super().fs_readfile(src, chunk_size, progress_callback)
            code = (
                "import os,websocket,webrepl\n"
                "_p=%r\n"
                "if os.stat(_p)[0]&0x4000:raise OSError(21)\n"
                "print(os.stat(_p)[6])\n"
                "_ws=websocket.websocket(webrepl.client_s,True)\n"
                "_ws.ioctl(%d,%d)\n"
                "_b=bytearray(%d);_m=memoryview(_b)\n"
                "with open(_p,'rb') as _f:\n"
                " while True:\n"
                "  _n=_f.readinto(_b)\n"
                "  if not _n:break\n"
                "  _ws.write(_m[:_n])\n"
                "del _ws,_b,_m\n"
            ) % (src, _SET_DATA_OPTS, _FRAME_BIN, GET_CHUNK)
            try:
                out = self.exec(code)
            except TransportExecError as e:
                raise _convert_filesystem_error(e, src) from None
            size = int(out.strip())
            data = self.serial.read_binary(size)
            if progress_callback:
                progress_callback(size, size)
            return bytearray(data)

        def _can_stream(self) -> bool:
            if self._stream_ok is None:
                try:
                    self._stream_ok = bool(
                        self.eval(
                            "bool(getattr(__import__('webrepl'),'client_s',None)) and "
                            "hasattr(__import__('websocket').websocket,'ioctl')"
                        )
                    )
                except Exception:
                    self._stream_ok = False
            return self._stream_ok

    _TRANSPORT_CLASS = WebReplTransport
    return WebReplTransport


def open_transport(device: str, password: Optional[str], **kwargs: Any) -> Any:
    """An mpremote ``SerialTransport`` whose "serial port" is a WebREPL socket."""
    cls = _transport_class()
    ws = WebSocketSerial(device, password, **kwargs)
    ws.open()
    return cls(ws)
