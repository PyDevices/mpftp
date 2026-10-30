"""The Connect dialog lists boards, not the UARTs Linux always creates (mpftp#179)."""

import os
import unittest
from unittest import mock

try:
    import serial.tools.list_ports  # noqa: F401

    HAVE_PYSERIAL = True
except ImportError:
    HAVE_PYSERIAL = False


def _port(device, vid=None, pid=None, description="n/a"):
    return mock.Mock(
        device=device,
        vid=vid,
        pid=pid,
        description=description,
        serial_number=None,
        manufacturer=None,
        product=None,
        interface=None,
        hwid="n/a",
    )


@unittest.skipUnless(HAVE_PYSERIAL, "pyserial not installed")
class ListPortsTests(unittest.TestCase):
    def _devices(self, ports):
        from mpftp import sidecar

        with mock.patch("serial.tools.list_ports.comports", return_value=ports), \
                mock.patch.dict(os.environ, {sidecar.EMULATOR_ENV: ""}):
            listed = sidecar.Session.list_ports(sidecar.Session.__new__(sidecar.Session))
        return [p["device"] for p in listed]

    def test_legacy_ttyS_ports_are_left_out(self):
        ports = [_port(f"/dev/ttyS{i}") for i in range(8)]
        self.assertEqual(self._devices(ports), [])

    def test_usb_boards_stay(self):
        ports = [_port("/dev/ttyS0"), _port("/dev/ttyACM0", 0x2E8A, 0x0005, "Board in FS mode")]
        self.assertEqual(self._devices(ports), ["/dev/ttyACM0"])

    def test_described_on_board_uart_stays(self):
        # A PC's real COM port (pnp) or a Pi's ttyAMA0 (amba) carries its name.
        self.assertEqual(self._devices([_port("/dev/ttyAMA0", description="ttyAMA0")]), ["/dev/ttyAMA0"])

    def test_windows_com_port_without_vid_stays(self):
        self.assertEqual(
            self._devices([_port("COM1", description="Communications Port (COM1)")]), ["COM1"]
        )


if __name__ == "__main__":
    unittest.main()
