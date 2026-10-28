"""Which CIRCUITPY drive belongs to the connected board (mpftp#169)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
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


class UartBridgedBoardTests(unittest.TestCase):
    """An ESP32-S3 on a CH343 bridge, next to two other boards' drives (mpftp#171)."""

    S3_UID = "F412FA4B2C10"

    def setUp(self):
        from mpftp import sidecar

        self._tmp = tempfile.TemporaryDirectory(prefix="mpftp-171-")
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.drives = []
        # An RP2040 Feather on H: and a Qualia on J:.
        for name, uid in (("H", "DF625857C74B5E2B"), ("J", "3485187A6B2C")):
            d = tmp / name
            d.mkdir()
            (d / "boot_out.txt").write_text(
                "Adafruit CircuitPython 9.2.1 on 2024-11-20\r\n"
                f"Board ID:some_board\r\nUID:{uid}\r\n"
            )
            self.drives.append(d)
        s = sidecar.Session()
        s.interpreter = "circuitpython"
        s.device = s.last_device = "COM17"
        s._find_circuitpy_host_roots = lambda: [str(d) for d in self.drives]
        self.ports = [
            {"device": "COM17", "vid": 0x1A86, "pid": 0x55D3, "serial_number": "5434012345"},
            {"device": "COM9", "vid": 0x239A, "pid": 0x80F2, "serial_number": "DF625857C74B5E2B"},
        ]
        s.list_ports = lambda: self.ports
        s.transport = mock.Mock(in_raw_repl=True)
        s.transport.exec.return_value = (self.S3_UID.lower() + "\r\n").encode()
        s._enter_raw = lambda *a, **k: None
        # Run board operations straight on the mock transport.
        s.with_raw = lambda fn, **k: fn(s.transport)
        s._transfer = lambda fn: fn
        self.s = s

    def _drive_files(self):
        return {d.name: sorted(p.name for p in d.iterdir()) for d in self.drives}

    def test_put_goes_over_the_repl_not_to_either_drive(self):
        res = self.s.fs_write("/settings.toml", "ZHVtbXkK")
        self.assertNotEqual(res.get("via"), "circuitpy_msc")
        self.s.transport.fs_writefile.assert_called_once()
        self.assertEqual(self._drive_files(), {"H": ["boot_out.txt"], "J": ["boot_out.txt"]})

    def test_every_write_path_stays_off_the_drives(self):
        for op in (
            lambda: self.s.fs_mkdir("/lib"),
            lambda: self.s.fs_touch("/x.txt"),
            lambda: self.s.fs_rm("/boot_out.txt"),
            lambda: self.s.fs_rm_rf("/boot_out.txt"),
            lambda: self.s.fs_rename("/boot_out.txt", "/y.txt"),
        ):
            try:
                op()
            except Exception:
                pass
        self.assertEqual(self._drive_files(), {"H": ["boot_out.txt"], "J": ["boot_out.txt"]})

    def test_the_bridges_serial_is_never_taken_for_the_board(self):
        # Even a bridge serial that happens to equal a drive's UID proves nothing.
        self.ports[0]["serial_number"] = "DF625857C74B5E2B"
        self.assertEqual(self.s._circuitpy_roots_for_device(), [])

    def test_a_bridged_board_with_its_own_drive_finds_it_by_its_uid(self):
        own = Path(self._tmp.name) / "K"
        own.mkdir()
        (own / "boot_out.txt").write_text(f"UID:{self.S3_UID}\n")
        self.drives.append(own)
        self.assertEqual(self.s._circuitpy_roots_for_device(), [str(own)])

    def test_a_uid_picked_out_of_other_output_is_not_trusted(self):
        self.s.transport.exec.return_value = b"df625857c74b5e2b\r\nwarning: something\r\n"
        self.s._board_uid_cache = None
        self.assertIsNone(self.s._board_uid())

    def test_a_uid_line_does_not_run_into_the_next(self):
        d = self.drives[0]
        (d / "boot_out.txt").write_text("UID:\nDF625857C74B5E2B\n")
        self.assertIsNone(self.s._circuitpy_uid(str(d)))

    def test_reconnecting_forgets_the_last_boards_uid(self):
        self.s._board_uid_cache = ("COM17", "DF625857C74B5E2B")
        self.s.transport = None
        self.s._force_close_transport()
        self.assertIsNone(self.s._board_uid_cache)


if __name__ == "__main__":
    unittest.main()
