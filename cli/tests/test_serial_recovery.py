"""Wedged-COM-handle recovery: bounded writes + dead-transport detection.

pyserial defaults to write_timeout=None (block forever). A COM handle left
over from a killed process can wedge WriteFile indefinitely with no exception
at all, hanging connect and every RPC queued behind it (mpftp#2) while the
session still claims connected: true (mpftp#3). No board required.
"""

from __future__ import annotations

import unittest
from unittest import mock


def _load_sidecar():
    from mpftp import sidecar

    return sidecar


class IsDeadSerialErrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_sidecar()

    def test_write_timeout_is_a_dead_serial_error(self):
        # Real exception is pyserial's SerialTimeoutException, but
        # is_dead_serial_error only ever inspects str(exc), so a plain
        # exception with the same message (pyserial isn't a test dependency
        # here) exercises the same code path.
        self.assertTrue(self.mod.is_dead_serial_error(RuntimeError("Write timeout")))

    def test_access_denied_is_still_a_dead_serial_error(self):
        self.assertTrue(self.mod.is_dead_serial_error(RuntimeError("PermissionError(13, 'Access is denied.')")))

    def test_unrelated_error_is_not_dead(self):
        self.assertFalse(self.mod.is_dead_serial_error(RuntimeError("could not enter raw repl")))


class BoundWriteTimeoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_sidecar()

    def test_sets_finite_write_timeout_on_the_transport_serial(self):
        transport = mock.Mock()
        transport.serial = mock.Mock(write_timeout=None)
        self.mod._bound_write_timeout(transport)
        self.assertEqual(transport.serial.write_timeout, self.mod.WRITE_TIMEOUT_SECS)

    def test_missing_serial_attribute_does_not_raise(self):
        transport = mock.Mock(spec=[])
        self.mod._bound_write_timeout(transport)  # no exception


class InterruptReclaimsOnWriteTimeoutTests(unittest.TestCase):
    """A wedged handle during ``interrupt`` should self-heal, not hang."""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_sidecar()

    def test_interrupt_reclaims_after_a_write_timeout(self):
        session = self.mod.Session()
        wedged_serial = mock.Mock()
        wedged_serial.write.side_effect = RuntimeError("Write timeout")
        wedged_transport = mock.Mock(serial=wedged_serial)
        session.transport = wedged_transport
        session.device = "COM99"
        session.last_device = "COM99"

        fresh_serial = mock.Mock()
        fresh_transport = mock.Mock(serial=fresh_serial)

        with mock.patch.object(self.mod, "_notify"), mock.patch.object(
            session, "_reclaim_session", return_value=fresh_transport
        ) as reclaim:
            result = session.interrupt()

        reclaim.assert_called_once_with(clean=False)
        fresh_serial.write.assert_called_once_with(b"\r\x03")
        self.assertEqual(result, {"ok": True, "reclaimed": True})
        # The wedged handle must be closed, not left dangling.
        wedged_serial.close.assert_called_once()


class _LineRecorder:
    """A serial stand-in that records control-line changes and close()."""

    def __init__(self):
        self.events = []
        self.open = True

    def __setattr__(self, name, value):
        if name in ("rts", "dtr"):
            self.events.append((name, value, self.open))
        object.__setattr__(self, name, value)

    def close(self):
        self.events.append(("close",))
        self.open = False


class ReleaseDoesNotResetTests(unittest.TestCase):
    """Releasing the port must not pulse an ESP's EN line (mpftp#77).

    Windows clears DTR before RTS when a port closes. With RTS still high
    that is EN low on an auto-reset circuit or a USB-Serial-JTAG, so a
    timed-out exec reset the board. Dropping RTS, then DTR, while the port is
    still open leaves nothing for the close to pulse.
    """

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_sidecar()

    def _release(self, serial):
        session = self.mod.Session()
        session.transport = mock.Mock(serial=serial)
        session.device = "COM99"
        with mock.patch.object(self.mod, "_notify"):
            session._release_dead_transport("timeout waiting for first EOF reception")

    def test_rts_then_dtr_drop_while_open_before_close(self):
        serial = _LineRecorder()
        self._release(serial)
        self.assertEqual(
            serial.events,
            [("rts", False, True), ("dtr", False, True), ("close",)],
        )

    def test_a_dead_handle_that_refuses_line_changes_still_closes(self):
        serial = mock.Mock()
        type(serial).rts = mock.PropertyMock(side_effect=OSError("dead"))
        type(serial).dtr = mock.PropertyMock(side_effect=OSError("dead"))
        self._release(serial)
        serial.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()


try:
    import serial.tools.list_ports  # noqa: F401

    HAVE_PYSERIAL = True
except ImportError:
    HAVE_PYSERIAL = False


@unittest.skipUnless(HAVE_PYSERIAL, "pyserial not installed")
class RefusedNativeUsbPortTests(unittest.TestCase):
    """mpftp#79: Windows refuses a board's own USB port while its program is stuck.

    MicroPython services TinyUSB from its scheduler, so a callback that never
    returns leaves the CDC port enumerated but unanswered, and every open fails
    with "Access is denied" although no process holds it. The error has to say
    so, or the only advice is to close a serial monitor that doesn't exist.
    """

    PORTS = [
        mock.Mock(device="COM4", vid=0x1A86, pid=0x55D3),    # CH343 bridge
        mock.Mock(device="COM42", vid=0x303A, pid=0x4001),   # MicroPython TinyUSB CDC
        mock.Mock(device="COM9", vid=0x303A, pid=0x1001),    # USB-Serial-JTAG
    ]
    DENIED = OSError("could not open port 'COM42': PermissionError(13, 'Access is denied.', None, 5)")

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_sidecar()

    def _message(self, device):
        with mock.patch("serial.tools.list_ports.comports", return_value=self.PORTS):
            return self.mod.Session._friendly_port_open_error(device, self.DENIED)

    def test_native_cdc_port_says_the_board_may_be_stuck(self):
        msg = self._message("COM42")
        self.assertIn("busy or locked", msg)
        self.assertIn("board itself may be stuck", msg)
        self.assertIn("hard-reset", msg)
        self.assertIn("Access is denied", msg)

    def test_uart_bridge_and_serial_jtag_keep_the_plain_message(self):
        for device in ("COM4", "COM9"):
            msg = self._message(device)
            self.assertIn("busy or locked", msg)
            self.assertNotIn("board itself may be stuck", msg)
