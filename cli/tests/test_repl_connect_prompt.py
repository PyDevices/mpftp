"""Opening the REPL pane right after connect shows no lone ``>`` (mpftp#150).

The connect handshake runs commands in raw REPL, and each one leaves the raw
prompt ``>`` unread. Leaving raw REPL for the pane used to forward it ahead
of the banner.
"""

from __future__ import annotations

import base64
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

    def write(self, data):
        pass

    def feed_later(self, data, delay=0.05):
        threading.Timer(delay, lambda: setattr(self, "rx", self.rx + data)).start()


class FakeTransport:
    def __init__(self):
        self.serial = FakeSerial()
        self.in_raw_repl = True

    def exit_raw_repl(self):
        self.in_raw_repl = False
        self.serial.feed_later(BANNER)


class ConnectPromptTests(unittest.TestCase):
    def test_pane_gets_the_banner_without_a_stray_prompt(self):
        s = sidecar.Session()
        t = FakeTransport()
        t.serial.rx = b">"  # left unread by the handshake's last exec
        s.transport = t
        seen = []
        with mock.patch.object(sidecar, "_notify", lambda m, p: seen.append((m, p))):
            s.repl_start()
            time.sleep(0.4)
            s._stop_repl_reader()
        shown = b"".join(
            base64.b64decode(p["data_b64"]) for m, p in seen if m == "repl_data"
        )
        self.assertEqual(shown, BANNER)


if __name__ == "__main__":
    unittest.main()
