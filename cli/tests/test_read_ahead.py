"""A download reads its reply in bulk, not a byte per serial call (mpftp#133)."""

from __future__ import annotations

import unittest

from mpftp.rawpaste import ReadAheadSerial


class CountingPort:
    def __init__(self, data: bytes):
        self.rx = bytearray(data)
        self.calls = 0
        self.dtr = False

    def inWaiting(self):
        self.calls += 1
        return len(self.rx)

    def read(self, n=1):
        self.calls += 1
        out = bytes(self.rx[:n])
        del self.rx[:n]
        return out


class ReadUntilTests(unittest.TestCase):
    def test_reply_is_taken_in_few_calls_and_the_rest_is_kept(self):
        reply = b"x" * 5000 + b"\x04" + b"\x04>"
        port = CountingPort(reply)
        s = ReadAheadSerial(port)
        first = s.read_until(b"\x04", 1, None, None)
        self.assertEqual(first, b"x" * 5000 + b"\x04")
        self.assertLess(port.calls, 5)
        # The bytes after the ending are still there, in order, for any reader.
        self.assertEqual(s.inWaiting(), 2)
        self.assertEqual(s.read(1), b"\x04")
        self.assertEqual(s.read_until(b">", 1, None, None), b">")

    def test_consumer_gets_everything_up_to_the_ending(self):
        port = CountingPort(b"abc\x04def")
        s = ReadAheadSerial(port)
        got = []
        s.read_until(b"\x04", 1, None, got.append)
        self.assertEqual(b"".join(got), b"abc\x04")
        self.assertEqual(s.read(3), b"def")

    def test_multibyte_ending_split_across_reads(self):
        port = CountingPort(b"raw REPL; CTRL-B to exit\r")
        s = ReadAheadSerial(port)
        port.rx.extend(b"\n>after")
        self.assertEqual(
            s.read_until(b"exit\r\n>", 1, None, None), b"raw REPL; CTRL-B to exit\r\n>"
        )
        self.assertEqual(s.read(5), b"after")

    def test_timeout_returns_what_arrived(self):
        s = ReadAheadSerial(CountingPort(b"abc"))
        self.assertEqual(s.read_until(b"\x04", 0.05, None, None), b"abc")

    def test_other_attributes_go_to_the_port(self):
        port = CountingPort(b"")
        s = ReadAheadSerial(port)
        s.dtr = True
        self.assertTrue(port.dtr)


if __name__ == "__main__":
    unittest.main()
