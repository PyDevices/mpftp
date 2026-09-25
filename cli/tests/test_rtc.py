"""The board clock on connect and on ``rtc --set`` (mpftp#58).

Every connect used to write the host's *local* time into the board's RTC,
which moved a clock NTP had set to UTC by the host's UTC offset. These tests
run the real board-side source in a sandbox whose ``machine``/``rtc``/``time``
modules are fakes holding a clock, so what they check is what the board would
end up holding, not which strings were sent.
"""

from __future__ import annotations

import ast
import builtins
import io
import time
import unittest
from unittest import mock

from test_helpers import _load_sidecar

# The host's clock during these tests: UTC and local differ by five hours, as
# in US Central where the bug was seen. Local is what the old code wrote.
HOST_UTC = time.struct_time((2026, 9, 25, 17, 30, 0, 4, 268, 0))
HOST_LOCAL = time.struct_time((2026, 9, 25, 12, 30, 0, 4, 268, 1))
HOST_UTC_RTC = [2026, 9, 25, 4, 17, 30, 0, 0]

# A clock NTP set a while ago, in machine.RTC().datetime() order.
NTP_SET = (2026, 9, 25, 4, 17, 29, 12, 0)
UNSET = (2000, 1, 1, 5, 0, 0, 3, 0)


class _StructTime(tuple):
    """CircuitPython's time.struct_time: built from one 9-tuple."""

    def __new__(cls, t):
        return super().__new__(cls, tuple(t))


class FakeBoard:
    """A raw REPL over a fake RTC, for MicroPython or CircuitPython.

    ``clock`` is always kept in machine.RTC().datetime() order. On
    CircuitPython the ``rtc`` module presents it as a struct_time property,
    as the real one does, and there is no ``machine`` module.
    """

    def __init__(self, interpreter: str, clock, *, has_rtc: bool = True):
        self.interpreter = interpreter
        self.clock = tuple(clock)
        self.has_rtc = has_rtc
        self.globals: dict = {}
        self.in_raw_repl = False
        board = self

        class MachineRTC:
            def datetime(self, t=None):
                if t is None:
                    return board.clock
                board.clock = tuple(t)

        class CircuitRTC:
            @property
            def datetime(self):
                y, mo, d, wd, h, mi, s, _ = board.clock
                return _StructTime((y, mo, d, h, mi, s, wd, -1, -1))

            @datetime.setter
            def datetime(self, st):
                y, mo, d, h, mi, s, wd = tuple(st)[:7]
                board.clock = (y, mo, d, wd, h, mi, s, 0)

        def localtime():
            y, mo, d, wd, h, mi, s, _ = board.clock
            return (y, mo, d, h, mi, s, wd, 0)

        self.modules = {
            "time": type("time", (), {
                "localtime": staticmethod(localtime),
                "struct_time": _StructTime,
            }),
        }
        if interpreter == "micropython" and has_rtc:
            self.modules["machine"] = type("machine", (), {"RTC": MachineRTC})
        if interpreter == "circuitpython" and has_rtc:
            self.modules["rtc"] = type("rtc", (), {"RTC": CircuitRTC})

    def exec(self, code, data_consumer=None):
        out = io.StringIO()
        modules = self.modules

        def _import(name, *args, **kwargs):
            if name in modules:
                return modules[name]
            raise ImportError(f"no module named '{name}'")

        def _print(*args, **kwargs):
            kwargs.pop("file", None)
            print(*args, file=out, **kwargs)

        sandbox = dict(vars(builtins))
        sandbox["__import__"] = _import
        sandbox["print"] = _print
        self.globals["__builtins__"] = sandbox
        exec(code, self.globals)
        return out.getvalue().encode()

    def eval(self, expression, parse=True):
        return ast.literal_eval(
            self.exec(f"print(repr({expression}))").decode().strip()
        )

    def user_globals(self) -> set:
        return {k for k in self.globals if k != "__builtins__"}


class RtcOnConnectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_sidecar()

    def setUp(self):
        patches = [
            mock.patch("time.gmtime", return_value=HOST_UTC),
            mock.patch("time.localtime", return_value=HOST_LOCAL),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _connect(self, board: FakeBoard):
        """What connect and resume run once the port is open."""
        session = self.mod.Session()
        session.interpreter = board.interpreter
        with mock.patch.object(session, "_take_control", lambda *a, **k: None):
            return session._probe_micropython(board)

    def _rtc_set(self, board: FakeBoard):
        session = self.mod.Session()
        session.interpreter = board.interpreter
        with mock.patch.object(session, "with_raw", lambda op: op(board)):
            return session.rtc_set()

    def _rtc_get(self, board: FakeBoard):
        session = self.mod.Session()
        session.interpreter = board.interpreter
        with mock.patch.object(session, "with_raw", lambda op: op(board)):
            return session.rtc_get()

    # -- MicroPython ---------------------------------------------------------

    def test_connect_leaves_an_ntp_set_micropython_clock_alone(self):
        board = FakeBoard("micropython", NTP_SET)
        rtc = self._connect(board)
        self.assertEqual(board.clock, NTP_SET)
        self.assertIsNone(rtc)

    def test_connect_sets_an_unset_micropython_clock_to_utc(self):
        board = FakeBoard("micropython", UNSET)
        rtc = self._connect(board)
        self.assertEqual(list(board.clock), HOST_UTC_RTC)
        self.assertEqual(rtc, HOST_UTC_RTC)

    def test_rtc_set_overwrites_a_set_micropython_clock_with_utc(self):
        board = FakeBoard("micropython", NTP_SET)
        self.assertEqual(self._rtc_set(board), {"datetime": HOST_UTC_RTC})
        self.assertEqual(list(board.clock), HOST_UTC_RTC)

    def test_connect_leaves_nothing_in_the_repl_globals(self):
        board = FakeBoard("micropython", UNSET)
        before = board.user_globals()
        self._connect(board)
        self.assertEqual(board.user_globals(), before)

    def test_connect_survives_a_port_without_an_rtc(self):
        board = FakeBoard("micropython", UNSET, has_rtc=False)
        self.assertIsNone(self._connect(board))

    # -- CircuitPython -------------------------------------------------------

    def test_connect_leaves_a_set_circuitpython_clock_alone(self):
        board = FakeBoard("circuitpython", NTP_SET)
        self.assertIsNone(self._connect(board))
        self.assertEqual(board.clock, NTP_SET)

    def test_connect_sets_an_unset_circuitpython_clock_to_utc(self):
        board = FakeBoard("circuitpython", UNSET)
        self.assertEqual(self._connect(board), HOST_UTC_RTC)
        self.assertEqual(list(board.clock), HOST_UTC_RTC)

    def test_rtc_set_and_get_on_circuitpython(self):
        # rtc.RTC().datetime is a property there; calling it raised
        # "'struct_time' object isn't callable" before, so both failed.
        board = FakeBoard("circuitpython", NTP_SET)
        self.assertEqual(self._rtc_set(board), {"datetime": HOST_UTC_RTC})
        self.assertEqual(self._rtc_get(board), {"datetime": HOST_UTC_RTC})


if __name__ == "__main__":
    unittest.main()
