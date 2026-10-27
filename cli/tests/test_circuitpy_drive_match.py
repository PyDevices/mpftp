"""Which CIRCUITPY drive belongs to the connected board (mpftp#169)."""

from __future__ import annotations

import unittest
from unittest import mock


class DriveMatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from mpftp import sidecar

        cls.mod = sidecar

    def _session(self, drives, serial=None, board_uid=None):
        """drives maps a root to the UID in its boot_out.txt."""
        s = self.mod.Session()
        s.interpreter = "circuitpython"
        s.device = s.last_device = "COM32"
        s._find_circuitpy_host_roots = lambda: list(drives)
        s._circuitpy_uid = lambda root: drives[root]
        s._device_usb_serial = lambda: serial
        asked = []

        def ask_board():
            asked.append(1)
            return board_uid

        s._board_uid = ask_board
        return s, asked

    def test_a_lone_drive_of_another_board_is_not_used(self):
        # The P4 on USB Serial/JTAG has no drive; the Pico's D: is the only one.
        s, _ = self._session({"D:\\": "495888255295DADE"}, board_uid="E8F60AE2C698")
        self.assertEqual(s._circuitpy_roots_for_device(), [])

    def test_a_board_that_cant_say_gets_no_drive(self):
        s, _ = self._session({"D:\\": "495888255295DADE"}, board_uid=None)
        self.assertEqual(s._circuitpy_roots_for_device(), [])

    def test_the_boards_own_uid_finds_its_drive(self):
        drives = {"D:\\": "495888255295DADE", "H:\\": "47543634370B5929"}
        s, _ = self._session(drives, board_uid="47543634370B5929")
        self.assertEqual(s._circuitpy_roots_for_device(), ["H:\\"])

    def test_a_known_serial_matches_without_asking_the_board(self):
        drives = {"D:\\": "495888255295DADE", "H:\\": "47543634370B5929"}
        s, asked = self._session(drives, serial="495888255295dade")
        self.assertEqual(s._circuitpy_roots_for_device(), ["D:\\"])
        self.assertEqual(asked, [])

    def test_no_drive_mounted_never_asks_the_board(self):
        s, asked = self._session({}, board_uid="E8F60AE2C698")
        self.assertEqual(s._circuitpy_roots_for_device(), [])
        self.assertEqual(asked, [])

    def test_the_board_uid_is_read_once_per_port(self):
        s = self.mod.Session()
        s.device = "COM32"
        s.transport = mock.Mock(in_raw_repl=True)
        s.transport.exec.return_value = b"495888255295dade\r\n"
        with mock.patch.object(s, "_enter_raw"):
            self.assertEqual(s._board_uid(), "495888255295DADE")
            self.assertEqual(s._board_uid(), "495888255295DADE")
        self.assertEqual(s.transport.exec.call_count, 1)

    def test_a_board_without_microcontroller_uid_reads_as_unknown(self):
        s = self.mod.Session()
        s.device = "COM32"
        s.transport = mock.Mock(in_raw_repl=True)
        s.transport.exec.side_effect = RuntimeError("AttributeError: uid")
        with mock.patch.object(s, "_enter_raw"):
            self.assertIsNone(s._board_uid())


if __name__ == "__main__":
    unittest.main()
