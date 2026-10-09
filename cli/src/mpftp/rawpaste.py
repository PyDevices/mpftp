"""Raw-paste over serial, one flow-control window in flight.

Raw-paste mode (``Ctrl-E A Ctrl-A``) lets the host run ahead of the board: the
board announces a window (128 bytes on MicroPython and CircuitPython), grants
two of them at the start, and sends ``\\x01`` each time it has read another
window's worth. mpremote uses both credits, so up to 256 bytes are on their
way while the board is still reading.

On CircuitPython's ESP32 ports, where TinyUSB runs in a FreeRTOS task of its
own, that overlap can corrupt the paste (mpftp#50). The VM task reading the
CDC FIFO can re-arm the OUT endpoint in the gap where TinyUSB's USB task has
marked the last transfer complete but not yet copied its 64-byte packet out
of the endpoint buffer (``lib/tinyusb/src/device/usbd.c`` clears ``busy``
before calling ``cdcd_xfer_cb``). The next packet from the host then lands on the one not
yet copied: that packet is lost and its successor arrives twice. On a T-Embed
(ESP32-S3) running CircuitPython 10.3.0, a 32,000-character paste came through
wrong 31 times in 120, each time as one 64-byte packet replaced by the next.

Sending one window and waiting for the board's ``\\x01`` before the next
closes that gap: the board asks for more only after it has read everything
sent, so no packet can arrive while an earlier one is still in the endpoint
buffer. The same 120 pastes sent this way all came through. It gives up a
little speed at best (43.8 KB/s against 54.8 KB/s for mpremote's way on its
good run; its other run managed 6.5 KB/s), so MicroPython boards keep
mpremote's way.
"""

from __future__ import annotations

import struct
from typing import Any, Callable, Optional

_TRANSPORT_CLASS: Any = None


def paced_raw_paste_write(transport: Any, command_bytes: bytes) -> None:
    """mpremote's ``raw_paste_write``, with at most one window unacknowledged."""
    try:
        from mpremote.transport import TransportError
    except ImportError:  # the sidecar always has mpremote; unit tests may not
        TransportError = RuntimeError  # noqa: N806

    serial = transport.serial
    header = serial.read(2)
    if len(header) != 2:
        raise TransportError("no raw-paste window from the board: {!r}".format(header))
    window = struct.unpack("<H", header)[0]
    if window == 0:
        raise TransportError("the board offered a raw-paste window of 0 bytes")

    # The board grants a second window straight after the header. Take the
    # byte off the wire and leave the credit unused.
    first = serial.read(1)
    if first == b"\x04":
        serial.write(b"\x04")
        return
    if first != b"\x01":
        raise TransportError("unexpected read during raw paste: {!r}".format(first))

    i = 0
    while i < len(command_bytes):
        chunk = command_bytes[i : i + window]
        serial.write(chunk)
        i += len(chunk)
        if i >= len(command_bytes):
            break
        # Wait until the board has read all of it.
        data = serial.read(1)
        if data == b"\x04":
            # The board stopped reading (a syntax error, say). Acknowledge.
            serial.write(b"\x04")
            return
        if data != b"\x01":
            raise TransportError("unexpected read during raw paste: {!r}".format(data))

    serial.write(b"\x04")
    data = transport.read_until(1, b"\x04")
    if not data.endswith(b"\x04"):
        raise TransportError("could not complete raw paste: {!r}".format(data))


def _transport_class() -> Any:
    global _TRANSPORT_CLASS
    if _TRANSPORT_CLASS is not None:
        return _TRANSPORT_CLASS

    from mpremote.transport_serial import SerialTransport

    class PacedSerialTransport(SerialTransport):
        """mpremote's serial transport; raw-paste waits for each window when
        ``paced()`` says so (the sidecar's: the board runs CircuitPython)."""

        paced: Optional[Callable[[], bool]] = None

        def __init__(
            self,
            device: str,
            baudrate: int = 115200,
            wait: int = 0,
            exclusive: bool = True,
            timeout: Optional[float] = None,
            lines_low: bool = False,
        ) -> None:
            if not lines_low:
                super().__init__(
                    device, baudrate=baudrate, wait=wait, exclusive=exclusive, timeout=timeout
                )
                return
            # mpremote's own setup, except that DTR and RTS are low before the
            # port opens and stay low. pyserial raises both as it opens; on a
            # USB-UART bridge wired to an ESP32's EN and IO0 the driver can
            # raise RTS first, which is EN low, and the board resets in the
            # middle of whatever it was running (mpftp#98).
            import serial as pyserial
            from mpremote.transport import TransportError

            self.in_raw_repl = False
            self.use_raw_paste = True
            self.device_name = device
            self.mounted = False
            kwargs: dict[str, Any] = {
                "baudrate": baudrate,
                "timeout": timeout,
                "interCharTimeout": 1,
            }
            if pyserial.__version__ >= "3.3":
                kwargs["exclusive"] = exclusive
            port = pyserial.serial_for_url(device, do_not_open=True, **kwargs)
            port.dtr = False
            port.rts = False
            try:
                port.open()
            except OSError as e:
                raise TransportError("failed to access " + device) from e
            self.serial = port

        def exec_raw_no_follow(self, command: Any) -> Any:
            # mpremote stops trying raw-paste for the rest of a connection
            # after one unexpected reply to its request, and sends everything
            # after that as plain raw REPL, 256 bytes every 10 ms. A UART into
            # CircuitPython drops bytes from that (mpftp#98), so on a paced
            # board ask again every time: one round trip, and the board's
            # flow control whenever it offers it.
            if self.paced is not None and self.paced():
                self.use_raw_paste = True
            return super().exec_raw_no_follow(command)

        def raw_paste_write(self, command_bytes: bytes) -> None:
            if self.paced is not None and self.paced():
                paced_raw_paste_write(self, command_bytes)
            else:
                super().raw_paste_write(command_bytes)

    _TRANSPORT_CLASS = PacedSerialTransport
    return _TRANSPORT_CLASS


def open_serial_transport(
    device: str, baud: int, paced: Callable[[], bool], lines_low: bool = False
) -> Any:
    """Open ``device``. ``lines_low`` keeps DTR and RTS low from before the
    port opens, for a USB-UART bridge whose lines reset the board."""
    if lines_low:
        transport = _transport_class()(device, baudrate=baud, lines_low=True)
    else:
        transport = _transport_class()(device, baudrate=baud)
    transport.paced = paced
    return transport
