"""Connecting to CircuitPython does not wait out timeouts (mpftp#155).

CircuitPython rebuilds its VM on every switch between the friendly and the raw
REPL, and drops what arrives meanwhile. Connect used to leave raw REPL and
enter it again straight away, and the next command did the same, so each of
those waited out a 10 s or 8 s timeout for a banner that never came. The fake
board in ``fake_circuitpython`` behaves that way, a little faster than a real
one so the tests stay short.
"""

from __future__ import annotations

import io
import time
import unittest
from contextlib import redirect_stderr
from unittest import mock

from fake_circuitpython import FakeCircuitPython
from mpftp import rawpaste, sidecar

RESTART = 0.3
STOP = 0.6


def _transport(board, session):
    cls = rawpaste._transport_class()
    t = cls.__new__(cls)
    t.in_raw_repl = False
    t.use_raw_paste = True
    t.device_name = "COM85"
    t.mounted = False
    t.is_pty = False
    t.paced = lambda: (session.interpreter or "") == "circuitpython"
    t.serial = board
    return t


class ConnectTimeTests(unittest.TestCase):
    def _connect(self, code_py: bool, stop_delay: float = STOP, deaf: float = 0.0):
        board = FakeCircuitPython(
            restart=RESTART, stop_delay=stop_delay, code_py=code_py, deaf=deaf
        )
        self.addCleanup(board.close)
        s = sidecar.Session()
        patches = [
            mock.patch.object(
                sidecar.Session, "_open_transport", lambda self, d, b: _transport(board, self)
            ),
            mock.patch.object(sidecar, "_notify", lambda *a, **k: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        t0 = time.monotonic()
        result = s.connect("COM85")
        return board, s, result, time.monotonic() - t0

    def test_board_at_the_prompt_connects_in_one_mode_switch(self):
        board, s, result, took = self._connect(code_py=False)
        self.assertEqual(result["interpreter"], "circuitpython")
        self.assertEqual(board.restarts, 1, board.timeline())
        self.assertLess(took, 2.5, board.timeline())

    def test_command_after_connect_costs_no_mode_switch(self):
        board, s, _, _ = self._connect(code_py=False)
        before = board.restarts
        t0 = time.monotonic()
        out = s.exec("print(1)")
        self.assertEqual(out["output"].strip(), "1")
        self.assertEqual(board.restarts, before, board.timeline())
        self.assertLess(time.monotonic() - t0, 1.0, board.timeline())

    def test_running_code_py_connects_without_retrying_out_the_timeouts(self):
        board, s, result, took = self._connect(code_py=True)
        self.assertEqual(result["interpreter"], "circuitpython")
        # code.py stops (STOP), the REPL starts (RESTART), raw REPL starts
        # (RESTART). The key that wakes the REPL must not cost a lost try.
        self.assertEqual(board.restarts, 2, board.timeline())
        self.assertLess(took, 3.0, board.timeline())

    def test_a_program_that_stops_inside_the_storm_costs_no_lost_try(self):
        # code.py stops while Ctrl-Cs are still going out, so one of them is
        # the key that starts the REPL, and a Ctrl-A sent straight after it
        # is lost while the REPL starts.
        board, s, result, took = self._connect(code_py=True, stop_delay=0.1)
        self.assertEqual(result["interpreter"], "circuitpython")
        self.assertEqual(board.restarts, 2, board.timeline())
        self.assertLess(took, 3.0, board.timeline())

    def test_a_lost_key_is_pressed_again(self):
        # The board misses the key sent straight after "Press any key", so
        # the REPL never starts unless it is pressed again.
        board, s, result, took = self._connect(code_py=True, deaf=0.3)
        self.assertEqual(result["interpreter"], "circuitpython")
        self.assertEqual(board.restarts, 2, board.timeline())
        self.assertLess(took, 4.0, board.timeline())

    def test_a_program_slower_to_stop_than_the_storm_costs_no_lost_try(self):
        # Nothing has printed when the storm ends, so a raw REPL try sent
        # then is spent as the key that starts the REPL.
        board, s, result, took = self._connect(code_py=True, stop_delay=1.2)
        self.assertEqual(result["interpreter"], "circuitpython")
        self.assertEqual(board.restarts, 2, board.timeline())
        self.assertLess(took, 3.5, board.timeline())

    def test_command_after_leaving_raw_waits_for_the_friendly_prompt(self):
        board, s, _, _ = self._connect(code_py=False)
        t = s.transport
        t.exit_raw_repl()
        # exit_raw_repl came back only once the board had printed its prompt,
        # and left the banner for whoever reads next.
        self.assertGreaterEqual(board.restarts, 2)
        self.assertTrue(t.serial.inWaiting() > 0)
        t0 = time.monotonic()
        t.enter_raw_repl(soft_reset=False, timeout_overall=8.0)
        self.assertLess(time.monotonic() - t0, RESTART + 1.0, board.timeline())


class PicoResetTests(unittest.TestCase):
    def test_rp2_port_is_not_pulsed_or_waited_on(self):
        class Port:
            port = "COM85"
            dtr = True
            rts = True

        port = Port()
        with mock.patch.object(sidecar, "port_can_be_esp_download", return_value=False):
            t0 = time.monotonic()
            sidecar.Session()._reset_esp_to_app(port)
        self.assertLess(time.monotonic() - t0, 0.3)
        self.assertTrue(port.dtr and port.rts)

    def test_vendor_ids(self):
        class P:
            def __init__(self, vid):
                self.device, self.vid = "X", vid

        for vid, expected in ((0x2E8A, False), (0x239A, False), (0x1A86, True), (0x303A, True)):
            with mock.patch("serial.tools.list_ports.comports", return_value=[P(vid)]):
                self.assertEqual(sidecar.port_can_be_esp_download("X"), expected, hex(vid))


class TraceTests(unittest.TestCase):
    def test_quiet_unless_asked(self):
        err = io.StringIO()
        with mock.patch.dict("os.environ", {}, clear=False) as env, redirect_stderr(err):
            env.pop("MPFTP_TRACE", None)
            sidecar.trace("a stage", restart=True)
        self.assertEqual(err.getvalue(), "")

    def test_stages_go_to_stderr_when_asked(self):
        err = io.StringIO()
        with mock.patch.dict("os.environ", {"MPFTP_TRACE": "1"}), redirect_stderr(err):
            sidecar.trace("first", restart=True)
            sidecar.trace("second")
        lines = err.getvalue().splitlines()
        self.assertRegex(lines[0], r"^mpftp trace \+0\.0\d\ds first$")
        self.assertRegex(lines[1], r"^mpftp trace \+\d+\.\d{3}s second$")


if __name__ == "__main__":
    unittest.main()
