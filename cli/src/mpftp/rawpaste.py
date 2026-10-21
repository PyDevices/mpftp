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


def _repr_line(out: bytes) -> bytes:
    """The printed ``repr`` in a board's reply: its first line, with the zeros
    some boards append to it taken off. A ``repr`` never holds a raw zero, CR
    or LF, so whatever follows its closing quote is not content."""
    line = out.split(b"\r", 1)[0].split(b"\n", 1)[0]
    return line.rstrip(b"\x00 ")


def read_file_checked(
    transport: Any,
    src: str,
    chunk_size: int = 256,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    attempts: int = 8,
) -> bytearray:
    """mpremote's ``fs_readfile``, except that a chunk that arrives wrong is
    asked for again.

    mpremote reads a file 256 bytes at a time as ``print(repr(r(256)))`` and
    parses the line it gets back. The P4 DEV-KIT's native USB sometimes sends
    more than it was given: after the closing quote come the zero padding of
    the string's memory and, now and then, the 16-byte header of the string
    object next to it, a byte of which can be the ``\\x04`` that ends the
    reply. A 2.8 MB model failed that way twice in two attempts ("source code
    string cannot contain null bytes"), and a third run lost the framing too.
    The bytes of the file inside the quotes were right every time, so each
    chunk is read with an explicit ``seek``, parsed from the first line only,
    and checked against the length it should have; a chunk that fails either
    is read again, up to ``attempts`` times.
    """
    import ast

    size = None
    for _ in range(attempts):
        try:
            size = transport.fs_stat(src).st_size
            break
        except OSError:
            raise
        except Exception as e:  # noqa: BLE001 - a stat reply with noise in it
            if _is_board_error(e):
                raise _convert_fs_error(transport, e, src) from None
            _resync(transport)
    if size is None:
        raise OSError("could not stat %s" % src)
    contents = bytearray()
    try:
        transport.exec("f=open('%s','rb')\nr=f.read\ns=f.seek" % src)
    except Exception as e:  # noqa: BLE001 - same conversion mpremote does
        raise _convert_fs_error(transport, e, src) from None
    try:
        while len(contents) < size:
            want = min(chunk_size, size - len(contents))
            chunk = None
            last: Optional[BaseException] = None
            for _ in range(attempts):
                try:
                    out = transport.exec(
                        "s(%d)\nprint(repr(r(%d)))" % (len(contents), want)
                    )
                    value = ast.literal_eval(_repr_line(out).decode("ascii"))
                except (ValueError, SyntaxError, UnicodeDecodeError, MemoryError) as e:
                    last = e
                    _resync(transport)
                    continue
                except Exception as e:  # noqa: BLE001
                    # A TransportExecError whose "error" is the spurious bytes
                    # (its text is decoded from them) or a framing timeout
                    # that the next command's wait for the prompt recovers.
                    if _is_board_error(e):
                        raise _convert_fs_error(transport, e, src) from None
                    last = e
                    _resync(transport)
                    continue
                if isinstance(value, (bytes, bytearray)) and len(value) == want:
                    chunk = bytes(value)
                    break
                last = ValueError(
                    "read %d bytes where %d were asked for"
                    % (len(value) if hasattr(value, "__len__") else -1, want)
                )
                _resync(transport)
            if chunk is None:
                raise OSError(
                    "reading %s at byte %d failed %d times: %s"
                    % (src, len(contents), attempts, last)
                )
            contents.extend(chunk)
            if progress_callback:
                progress_callback(len(contents), size)
    finally:
        try:
            transport.exec("f.close()")
        except Exception:  # noqa: BLE001 - the read's own error is the one to show
            pass
    return contents


def _resync(transport: Any) -> None:
    """Back to a clean raw-REPL prompt after a reply that did not parse. The
    spurious bytes can include a ``\\x04``, which ends the reply early and leaves
    the real end marker and prompt unread. Entering raw REPL again (no soft
    reset, so the open file stays open) throws all of that away."""
    enter = getattr(transport, "enter_raw_repl", None)
    if enter is not None:
        try:
            enter(soft_reset=False)
        except Exception:  # noqa: BLE001 - the retry will say if it did not work
            pass


def _is_board_error(exc: BaseException) -> bool:
    """True for an error the board reported about the file (a Python
    traceback), not for noise in the reply."""
    text = str(getattr(exc, "error_output", "") or "")
    return "Traceback" in text


def _convert_fs_error(transport: Any, exc: BaseException, src: str) -> BaseException:
    try:
        from mpremote.transport import _convert_filesystem_error

        return _convert_filesystem_error(exc, src) or exc
    except Exception:  # noqa: BLE001
        return exc


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
            # mpremote 1.29 reads is_pty when it waits for the board; its own
            # __init__, skipped here, sets it from the device path.
            is_pty = getattr(SerialTransport, "_is_pty_device", None)
            self.is_pty = bool(is_pty(device)) if is_pty else False
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

        def fs_readfile(self, src, chunk_size=256, progress_callback=None):
            return read_file_checked(self, src, chunk_size, progress_callback)

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
