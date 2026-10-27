"""A transfer leaves the REPL pane alone (mpftp#134).

Leaving raw REPL sends Ctrl-B, and the board answers with its banner and a
fresh prompt. The REPL reader used to forward that to the pane after every
upload and download. The session now reads the greeting itself and drops it.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from mpftp import sidecar

BANNER = (
    b"\r\nMicroPython v1.25.0 on 2025-04-15; Generic with ESP32\r\n"
    b'Type "help()" for more information.\r\n>>> '
)


class FakeSerial:
    def __init__(self):
        self.rx = b""
        self.lock = threading.Lock()

    def inWaiting(self):
        return len(self.rx)

    def read(self, n):
        with self.lock:
            out, self.rx = self.rx[:n], self.rx[n:]
        return out

    def feed_later(self, data, delay=0.05):
        threading.Timer(delay, lambda: setattr(self, "rx", self.rx + data)).start()


class FakeTransport:
    def __init__(self):
        self.serial = FakeSerial()
        self.in_raw_repl = True

    def enter_raw_repl(self, **_kw):
        self.in_raw_repl = True

    def exit_raw_repl(self):
        self.in_raw_repl = False
        self.serial.feed_later(BANNER)


class QuietTransferTests(unittest.TestCase):
    def test_banner_after_a_board_operation_stays_out_of_the_pane(self):
        s = sidecar.Session()
        t = FakeTransport()
        s.transport = t
        s._repl_mode = True
        seen = []
        with mock.patch.object(sidecar, "_notify", lambda m, p: seen.append((m, p))):
            s._enter_raw = lambda soft_reset=False: setattr(t, "in_raw_repl", True)
            s.with_raw(lambda tr: 1)
            time.sleep(0.3)
            s._stop_repl_reader()
        self.assertFalse(t.in_raw_repl)
        self.assertEqual(seen, [])
        self.assertEqual(t.serial.rx, b"")


if __name__ == "__main__":
    unittest.main()
