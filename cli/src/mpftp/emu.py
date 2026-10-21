"""
The desktop app's emulator as a board: ``emu://``.

The emulator is MicroPython compiled to WebAssembly, running in its own
window of the desktop app. To the sidecar it is a board on a serial line:
``EmuSerial`` has the pyserial surface mpremote's ``SerialTransport`` uses,
and the bytes travel as messages instead of over a wire.

    sidecar -> emulator   notify {"method": "emu_out", "params": {"b64": ...}}
    emulator -> sidecar   a stdin line {"emu_in": "<base64>"}

The sidecar's own loop runs one request at a time, and a request (an
upload, say) blocks waiting for the emulator's reply. So emulator lines
never queue behind requests: the sidecar's stdin reader hands them to
``deliver()`` the moment they arrive.

The rest of the conversation:

    notify emu_open        the sidecar wants the board: the app opens the
                           window if it isn't open, and the window answers
    {"emu_ready": true}    the interpreter is up and taking bytes
    {"emu_gone": true}     the window closed: the board is unplugged
"""

from __future__ import annotations

import base64
import threading
import time
from typing import Any, Callable, Optional

PREFIX = "emu://"
DEFAULT_DEVICE = "emu://emulator"
START_TIMEOUT = 30.0


class EmuError(OSError):
    pass


def _closed(port: str, why: str) -> EmuError:
    # "port is closed" is what the sidecar looks for to drop a dead board.
    return EmuError(f"{port}: port is closed ({why})")


def is_emu_device(device: Optional[str]) -> bool:
    return bool(device) and str(device).startswith(PREFIX)


def port_entry() -> dict[str, Any]:
    """The emulator's row in list_ports, shaped like a serial port's."""
    return {
        "device": DEFAULT_DEVICE,
        "serial_number": None,
        "vid": None,
        "pid": None,
        "manufacturer": "PyDevices",
        "product": "Emulator",
        "description": "Emulator (MicroPython in a window)",
        "interface": None,
        "hwid": None,
        "repl": True,
    }


#: The open link, if any. deliver() routes the emulator's lines to it.
_link: Optional["EmuSerial"] = None
_link_lock = threading.Lock()


def deliver(msg: dict[str, Any]) -> None:
    """One emulator line from the sidecar's stdin reader thread."""
    with _link_lock:
        link = _link
    if link is None:
        return
    if "emu_in" in msg:
        try:
            link._feed(base64.b64decode(msg["emu_in"] or ""))
        except (ValueError, TypeError):
            pass
    if msg.get("emu_ready"):
        link._ready()
    if msg.get("emu_gone"):
        link._gone("the emulator window was closed")


class EmuSerial:
    """The pyserial surface mpremote's ``SerialTransport`` uses, over messages."""

    def __init__(self, device: str, notify: Callable[[str, dict[str, Any]], None]) -> None:
        self.port = device
        self._notify = notify
        self.timeout: Optional[float] = None
        self.write_timeout: Optional[float] = None
        self.dtr = self.rts = False
        self._rx = bytearray()
        self._cond = threading.Condition()
        self._started = False
        self._closed_reason: Optional[str] = None

    # Opening and closing

    def open(self) -> None:
        global _link
        with _link_lock:
            old, _link = _link, self
        if old is not None and old is not self:
            old._gone("replaced by a new connection")
        self._notify("emu_open", {"device": self.port})
        deadline = time.monotonic() + START_TIMEOUT
        with self._cond:
            while not self._started and self._closed_reason is None:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(min(left, 0.5))
            if self._closed_reason:
                raise EmuError(f"{self.port}: {self._closed_reason}")
            if not self._started:
                raise EmuError(f"{self.port}: the emulator didn't start in {START_TIMEOUT:.0f} s")

    def close(self) -> None:
        global _link
        with _link_lock:
            if _link is self:
                _link = None
        with self._cond:
            self._closed_reason = self._closed_reason or "closed by mpftp"
            self._cond.notify_all()

    @property
    def is_open(self) -> bool:
        return self._closed_reason is None

    def isOpen(self) -> bool:  # noqa: N802 (pyserial spelling)
        return self.is_open

    # From the emulator (the stdin reader thread)

    def _feed(self, data: bytes) -> None:
        if data:
            with self._cond:
                self._rx.extend(data)
                self._cond.notify_all()

    def _ready(self) -> None:
        with self._cond:
            self._started = True
            self._cond.notify_all()

    def _gone(self, why: str) -> None:
        with self._cond:
            self._closed_reason = self._closed_reason or why
            self._cond.notify_all()

    # pyserial

    def setDTR(self, value: bool = True) -> None:  # noqa: N802
        self.dtr = value

    def setRTS(self, value: bool = True) -> None:  # noqa: N802
        self.rts = value

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        with self._cond:
            self._rx.clear()

    flushInput = reset_input_buffer  # noqa: N815

    @property
    def in_waiting(self) -> int:
        return self.inWaiting()

    def inWaiting(self) -> int:  # noqa: N802
        with self._cond:
            if not self._rx and self._closed_reason:
                raise _closed(self.port, self._closed_reason)
            return len(self._rx)

    def read(self, size: int = 1) -> bytes:
        # Like a serial port, not a radio link: with no timeout, wait as long
        # as it takes. A running program can be silent for good.
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        with self._cond:
            while len(self._rx) < size:
                if self._closed_reason:
                    if self._rx:
                        break
                    raise _closed(self.port, self._closed_reason)
                if deadline is None:
                    self._cond.wait(0.5)
                    continue
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(min(left, 0.5))
            out = bytes(self._rx[:size])
            del self._rx[:size]
            return out

    def write(self, data: bytes) -> int:
        data = bytes(data)
        if self._closed_reason:
            raise _closed(self.port, self._closed_reason)
        if data:
            self._notify("emu_out", {"b64": base64.b64encode(data).decode("ascii")})
        return len(data)


_TRANSPORT_CLASS: Any = None


def _transport_class() -> Any:
    global _TRANSPORT_CLASS
    if _TRANSPORT_CLASS is not None:
        return _TRANSPORT_CLASS

    from mpremote.transport_serial import SerialTransport

    class EmuTransport(SerialTransport):
        """mpremote's serial transport with the emulator as its port.

        No raw-paste: the WebAssembly interpreter's REPL is event driven, and
        raw-paste there reads its input with a blocking read the browser can't
        give it. Plain raw REPL works.
        """

        def __init__(self, link: EmuSerial) -> None:  # pyserial is not used
            self.in_raw_repl = False
            self.use_raw_paste = False
            self.device_name = link.port
            self.mounted = False
            self.serial = link

    _TRANSPORT_CLASS = EmuTransport
    return EmuTransport


def open_transport(device: str, notify: Callable[[str, dict[str, Any]], None]) -> Any:
    link = EmuSerial(device, notify)
    link.open()
    return _transport_class()(link)
