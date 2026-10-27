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


class PromptingSerial(FakeSerial):
    """A board that answers a Return with a fresh prompt."""

    def __init__(self):
        super().__init__()
        self.sent = []

    def write(self, data):
        self.sent.append(data)
        if data == b"\r":
            self.feed_later(b"\r\n>>> ", 0.01)


class PromptingTransport(FakeTransport):
    def __init__(self):
        self.serial = PromptingSerial()
        self.in_raw_repl = True


class OnePromptTests(unittest.TestCase):
    def _shown_after_connect(self):
        s = sidecar.Session()
        t = PromptingTransport()
        s.transport = t
        seen = []
        with (
            mock.patch.object(sidecar, "_notify", lambda m, p: seen.append((m, p))),
            mock.patch.object(s, "_take_control"),
            mock.patch.object(s, "_apply_rtc", return_value=None),
        ):
            s._probe_micropython(t)  # connect leaves raw REPL: banner + prompt
            s.repl_start()
            time.sleep(0.4)
            s._stop_repl_reader()
        shown = b"".join(
            base64.b64decode(p["data_b64"]) for m, p in seen if m == "repl_data"
        )
        return t, shown

    def test_opening_the_pane_after_connect_shows_one_prompt(self):
        t, shown = self._shown_after_connect()
        self.assertEqual(shown.count(b">>> "), 1, shown)
        self.assertNotIn(b"\r", t.serial.sent)

    def test_a_later_pane_still_asks_for_a_prompt(self):
        s = sidecar.Session()
        t = PromptingTransport()
        t.in_raw_repl = False
        s.transport = t
        with mock.patch.object(sidecar, "_notify", lambda m, p: None):
            s.repl_start()
            s._stop_repl_reader()
        self.assertEqual(t.serial.sent, [b"\r"])


if __name__ == "__main__":
    unittest.main()
