"""A board that floods output forever must not hold connect indefinitely (mpftp#193).

mpremote's timeouts run between bytes, so continuous output defeats them.
No board required: a fake transport streams forever.
"""

from __future__ import annotations

import os
import time
import unittest
from unittest import mock


class FloodingSerial:
    """Always has a byte to give, like a Timer printing every few ms."""

    def __init__(self):
        self.writes = []
        self.dtr = False

    def inWaiting(self):
        return 64

    def read(self, n=1):
        time.sleep(0.001)
        return b"tick tick tick\r\n"[:n] if n < 16 else b"tick tick tick\r\n"

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def close(self):
        pass


class FloodingTransport:
    def __init__(self):
        self.serial = FloodingSerial()
        self.in_raw_repl = False
        self.closed = False

    def enter_raw_repl(self, soft_reset=True, timeout_overall=10.0):
        # mpremote's read_until: restarts its per-byte timer on every byte.
        while True:
            self.serial.read(1)

    def close(self):
        self.closed = True


class ConnectDeadlineTests(unittest.TestCase):
    def test_endless_flood_ends_with_a_reset_button_message(self):
        from mpftp import sidecar

        s = sidecar.Session()
        t = FloodingTransport()
        started = time.monotonic()
        with mock.patch.dict(os.environ, {"MPFTP_CONNECT_DEADLINE": "1.5"}), \
                mock.patch.object(s, "_open_transport", return_value=t), \
                mock.patch.object(s, "_serial_peek", return_value=b""), \
                self.assertRaises(RuntimeError) as cm:
            s.connect("COM99", attempts=3)
        took = time.monotonic() - started
        msg = str(cm.exception)
        self.assertIn("RESET button", msg)
        self.assertIn("kept sending output", msg)
        self.assertLess(took, 6)
        self.assertTrue(t.closed)
        self.assertIsNone(s.transport)

    def test_the_port_is_unwrapped_after_a_good_connect(self):
        from mpftp import sidecar

        s = sidecar.Session()
        t = FloodingTransport()
        raw = t.serial
        with mock.patch.object(s, "_open_transport", return_value=t), \
                mock.patch.object(s, "_probe_micropython", return_value=None):
            s.connect("COM99", attempts=1)
        self.assertIs(t.serial, raw)


if __name__ == "__main__":
    unittest.main()
