"""``get`` survives a board that sends more than it was asked to print.

The P4 DEV-KIT, on its native USB, sometimes sends bytes after the closing
quote of a printed ``repr``: the zero padding of the string's memory, and now
and then the 16-byte header of the string object beside it. A byte of that
header can be ``\\x04``, which ends the reply early. mpremote parsed the line
with ``ast.literal_eval`` and failed on the zeros ("source code string cannot
contain null bytes"), once in a few hundred thousand bytes. These tests give
``read_file_checked`` a stand-in board that does the same and check that the
file comes out byte for byte.
"""

from __future__ import annotations

import contextlib
import io
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mpftp import rawpaste

try:
    from mpremote.transport import TransportExecError

    HAVE_MPREMOTE = True
except ImportError:
    HAVE_MPREMOTE = False

    class TransportExecError(Exception):  # type: ignore[no-redef]
        def __init__(self, status_code, error_output):
            self.status_code = status_code
            self.error_output = error_output
            super().__init__(error_output)


def blob() -> bytes:
    rng = random.Random(7)
    return bytes(range(256)) + bytes(700) + bytes(rng.randrange(256) for _ in range(4000)) + b"\x04\x00"


class FaultyBoard:
    """Runs the commands under CPython; ``fault(n, out)`` may change reply n."""

    def __init__(self, path: str, fault=None) -> None:
        self.path = path
        self.ns: dict = {}
        self.fault = fault
        self.calls = 0
        self.resyncs = 0

    def fs_stat(self, src):
        return SimpleNamespace(st_size=Path(self.path).stat().st_size)

    def enter_raw_repl(self, soft_reset=True):
        assert soft_reset is False, "a soft reset would close the open file"
        self.resyncs += 1

    def exec(self, command, data_consumer=None):
        command = command.replace(repr(self.path)[1:-1], self.path)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(compile(command, "<board>", "exec"), self.ns)
        out = buf.getvalue().replace("\n", "\r\n").encode()
        if "print(repr(r(" in command:
            self.calls += 1
            if self.fault is not None:
                return self.fault(self.calls, out)
        return out


@unittest.skipUnless(HAVE_MPREMOTE, "mpremote not installed")
class ReadFileCheckedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "net.espdl")
        self.data = blob()
        Path(self.path).write_bytes(self.data)

    def read(self, fault=None, **kw):
        board = FaultyBoard(self.path, fault)
        return board, rawpaste.read_file_checked(board, self.path, **kw)

    def test_a_clean_board_gives_the_file(self):
        board, got = self.read()
        self.assertEqual(bytes(got), self.data)
        self.assertEqual(board.resyncs, 0)

    def test_zeros_after_the_closing_quote(self):
        # What the board sent: the repr, 16 zeros, then the newline.
        def fault(n, out):
            return out[:-2] + bytes(16) + b"\r\n" if n % 5 == 0 else out

        board, got = self.read(fault)
        self.assertEqual(bytes(got), self.data)

    def test_zeros_and_the_next_objects_header_with_its_04(self):
        # type pointer 0x406b3704, hash, length, data pointer: the stream then
        # carries \x04 early, mpremote ends the reply there and reads the rest
        # as the error text.
        header = b"\x047k@=|\x00\x00\xc4\x03\x00\x00\xe0\x93\x00H"

        def fault(n, out):
            if n == 3:
                raise TransportExecError(out[:-2] + bytes(12), header[1:].decode("latin-1") + "\r\n")
            return out

        board, got = self.read(fault)
        self.assertEqual(bytes(got), self.data)
        self.assertEqual(board.resyncs, 1)

    def test_a_reply_cut_short_is_read_again(self):
        def fault(n, out):
            return out[:40] + b"'\r\n" if n == 4 else out

        board, got = self.read(fault)
        self.assertEqual(bytes(got), self.data)

    def test_a_reply_that_never_parses_gives_up_and_says_where(self):
        def fault(n, out):
            return b"b'\\x00" + bytes(8) if n >= 4 else out

        with self.assertRaises(OSError) as cm:
            self.read(fault, attempts=3)
        self.assertIn("at byte 768", str(cm.exception))

    def test_a_board_error_is_not_retried(self):
        board = FaultyBoard(self.path)
        Path(self.path).unlink()
        board.fs_stat = lambda src: SimpleNamespace(st_size=10)
        with self.assertRaises(Exception) as cm:
            rawpaste.read_file_checked(board, self.path)
        self.assertEqual(board.calls, 0)
        self.assertIsInstance(cm.exception, (OSError, FileNotFoundError))

    def test_the_file_is_not_a_multiple_of_the_chunk(self):
        Path(self.path).write_bytes(self.data[:1000])
        _, got = self.read()
        self.assertEqual(bytes(got), self.data[:1000])
        Path(self.path).write_bytes(b"")
        _, got = self.read()
        self.assertEqual(bytes(got), b"")

    def test_repr_line_drops_the_padding_only(self):
        self.assertEqual(rawpaste._repr_line(b"b'a\\x00'\x00\x00\x00\r\n"), b"b'a\\x00'")


if __name__ == "__main__":
    unittest.main()
