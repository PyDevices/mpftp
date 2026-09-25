"""Raw-paste pacing for CircuitPython over serial (mpftp#50)."""

from __future__ import annotations

import unittest
from unittest import mock

from mpftp import rawpaste

try:
    import mpremote  # noqa: F401

    HAVE_MPREMOTE = True
except ImportError:
    HAVE_MPREMOTE = False

WINDOW = 128


class FakeBoard:
    """The board's side of raw-paste, as shared/runtime/pyexec.c does it.

    It announces a 128-byte window and one extra credit, reads what the host
    wrote whenever the host waits on it, and sends ``\\x01`` for every window
    it has read. ``most_in_flight`` is the largest amount the host had
    written that the board had not read yet: what the board's USB receive
    path has to hold at once.
    """

    def __init__(self, stop_after: int | None = None) -> None:
        self.out = bytearray(bytes([WINDOW & 0xFF, WINDOW >> 8, 0x01]))
        self.received = bytearray()
        self.read_upto = 0
        self.since_ack = 0
        self.most_in_flight = 0
        self.finished = False
        self.stop_after = stop_after
        self.stopped = False

    # pyserial surface
    def write(self, data: bytes) -> int:
        if self.stopped:
            if data == b"\x04":
                self.finished = True
            return len(data)
        self.received += data
        self.most_in_flight = max(self.most_in_flight, len(self.received) - self.read_upto)
        if self.received.endswith(b"\x04"):
            self._consume()
        return len(data)

    def _consume(self) -> None:
        while self.read_upto < len(self.received):
            c = self.received[self.read_upto]
            self.read_upto += 1
            if c == 0x04:
                self.finished = True
                self.out += b"\x04"
                return
            if self.stop_after is not None and self.read_upto >= self.stop_after:
                self.stopped = True
                self.out += b"\x04"
                return
            self.since_ack += 1
            if self.since_ack == WINDOW:
                self.since_ack = 0
                self.out += b"\x01"

    def read(self, n: int = 1) -> bytes:
        if not self.out:
            self._consume()
        data = bytes(self.out[:n])
        del self.out[:n]
        return data

    def inWaiting(self) -> int:  # noqa: N802 (pyserial spelling)
        return len(self.out)


class FakeTransport:
    def __init__(self, board: FakeBoard) -> None:
        self.serial = board

    def read_until(self, min_num_bytes: int, ending: bytes, timeout: float = 10) -> bytes:
        data = bytearray()
        while not data.endswith(ending):
            chunk = self.serial.read(1)
            if not chunk:
                break
            data += chunk
        return bytes(data)


def _source(n: int) -> bytes:
    return bytes(0x41 + (i % 26) for i in range(n))


class PacedRawPasteTests(unittest.TestCase):
    def test_everything_arrives_in_order(self) -> None:
        for n in (1, WINDOW - 1, WINDOW, WINDOW + 1, 5 * WINDOW, 24_000):
            board = FakeBoard()
            src = _source(n)
            rawpaste.paced_raw_paste_write(FakeTransport(board), src)
            self.assertTrue(board.finished, n)
            self.assertEqual(bytes(board.received), src + b"\x04", n)

    def test_never_more_than_one_window_unread(self) -> None:
        board = FakeBoard()
        rawpaste.paced_raw_paste_write(FakeTransport(board), _source(24_000))
        self.assertLessEqual(board.most_in_flight, WINDOW + 1)  # + the closing Ctrl-D

    @unittest.skipUnless(HAVE_MPREMOTE, "mpremote not installed")
    def test_mpremotes_own_write_runs_two_windows_ahead(self) -> None:
        # The control: the same fake board sees mpremote's stock writer put
        # two windows in flight, which is what garbles CircuitPython (#50).
        from mpremote.transport_serial import SerialTransport

        board = FakeBoard()
        t = FakeTransport(board)
        SerialTransport.raw_paste_write(t, _source(24_000))  # type: ignore[arg-type]
        self.assertEqual(bytes(board.received), _source(24_000) + b"\x04")
        self.assertGreater(board.most_in_flight, WINDOW + 1)

    def test_board_that_stops_reading_is_acknowledged(self) -> None:
        board = FakeBoard(stop_after=300)
        rawpaste.paced_raw_paste_write(FakeTransport(board), _source(2_000))
        self.assertTrue(board.finished)
        self.assertLess(len(board.received), 2_000)


@unittest.skipUnless(HAVE_MPREMOTE, "mpremote not installed")
class TransportChoiceTests(unittest.TestCase):
    def test_paces_only_when_told(self) -> None:
        cls = rawpaste._transport_class()
        t = cls.__new__(cls)
        t.paced = lambda: False
        with mock.patch.object(rawpaste, "paced_raw_paste_write") as paced, \
                mock.patch("mpremote.transport_serial.SerialTransport.raw_paste_write") as stock:
            t.raw_paste_write(b"x")
            self.assertEqual((paced.call_count, stock.call_count), (0, 1))
            t.paced = lambda: True
            t.raw_paste_write(b"x")
            self.assertEqual((paced.call_count, stock.call_count), (1, 1))

    def test_sidecar_paces_circuitpython_only(self) -> None:
        from mpftp.sidecar import Session

        s = Session.__new__(Session)
        s.interpreter = None
        with mock.patch.object(rawpaste, "open_serial_transport", side_effect=lambda d, b, paced: paced):
            paced = s._open_transport("COM25", 115200)
        self.assertFalse(paced())
        s.interpreter = "micropython"
        self.assertFalse(paced())
        s.interpreter = "circuitpython"
        self.assertTrue(paced())


if __name__ == "__main__":
    unittest.main()
