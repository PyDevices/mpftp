"""An ESP32-S2 in ROM download mode on its own USB is detected and flashed
without being stranded (mpftp#70).

The S2's ROM keeps its USB connection through a chip reset, so when esptool
resets it -- the RTS hard reset, or leaving the flasher stub -- the S2 starts
its firmware while the host still holds the ROM's port (303A:0002), and that
port never answers again until someone presses RESET. Each test plants the
command line that stranded the board.
"""

from __future__ import annotations

import argparse
import json
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mpftp import firmware


def setUpModule():
    # flash_esp32 records each flash in ~/.mpftp/activity.log, where a test's
    # made-up port would read as a real flash of whatever board is on it.
    patcher = mock.patch.object(firmware, "log_activity")
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)

S2_ROM = (0x303A, 0x0002)
MICROPYTHON_CDC = (0x303A, 0x4001)

S2_FLASH_ID = """\
esptool v5.3.1
Connected to ESP32-S2 on COM29:
Chip type:          ESP32-S2 (revision v0.0)
Features:           Wi-Fi, Single Core, 240MHz, No Embedded Flash, No Embedded PSRAM
Crystal frequency:  40MHz
USB mode:           USB-OTG
MAC:                7c:df:a1:17:8e:a2
Detected flash size: 4MB
Staying in bootloader.
"""


def resets_the_chip(cmd: list[str]) -> list[str]:
    """What in ``cmd`` makes esptool reset an S2 sitting in its ROM loader."""
    found = []
    if "--before" in cmd and cmd[cmd.index("--before") + 1] != "no-reset":
        found.append("--before " + cmd[cmd.index("--before") + 1])
    after = cmd[cmd.index("--after") + 1] if "--after" in cmd else "hard-reset"
    if after == "hard-reset":
        found.append("--after hard-reset")
    if after == "no-reset" and "--no-stub" not in cmd:
        found.append("leaving the stub with --after no-reset")
    return found


class DetectTests(unittest.TestCase):
    def run_detect(self, usb_id):
        ns = argparse.Namespace(device="COM29", mp_hints="", mp="", esptool=None)
        calls = []

        def capture(_ns, args, timeout=60):
            calls.append(list(args))
            if args[-1] == "flash-id":
                return 0, S2_FLASH_ID
            return 0, "Secure Boot: Disabled\nFlash Encryption: Disabled\n"

        with mock.patch.object(firmware, "_port_usb_id", return_value=usb_id), \
                mock.patch.object(firmware, "_esptool_capture", side_effect=capture), \
                mock.patch.object(firmware, "save_state"), \
                mock.patch.object(firmware, "log_activity"), \
                mock.patch.object(firmware, "print_json") as printed:
            firmware.do_detect(ns)
        return calls, printed.call_args[0][0]

    def test_s2_rom_port_is_never_reset(self):
        calls, result = self.run_detect(S2_ROM)
        self.assertEqual([c[-1] for c in calls], ["flash-id", "get-security-info"])
        for cmd in calls:
            self.assertEqual(resets_the_chip(cmd), [], cmd)
        self.assertEqual(result["chip"], "ESP32-S2")
        self.assertTrue(result["security"]["available"])

    def test_other_ports_still_hard_reset(self):
        # A P4 behind a USB-UART bridge stays in "waiting for download" unless
        # detect resets it afterwards.
        for usb_id in (None, MICROPYTHON_CDC, (0x1A86, 0x55D3)):
            calls, _ = self.run_detect(usb_id)
            for cmd in calls:
                self.assertEqual(cmd[cmd.index("--after") + 1], "hard-reset")
                self.assertEqual(cmd[cmd.index("--before") + 1], "default-reset")


def combined_s2() -> bytes:
    header = bytes([0xE9, 1, 2, 0x20]) + b"\x00" * 8 + struct.pack("<H", 0x0002)
    return header + b"\x00" * 64


class FlashTests(unittest.TestCase):
    def run_flash(self, usb_id, before="", after="", layout=None):
        with tempfile.TemporaryDirectory() as tmp:
            fw = Path(tmp) / "firmware.bin"
            fw.write_bytes(combined_s2())
            ns = argparse.Namespace(
                port="esp32", board="", family="", offset="", device="COM29",
                baud=460800, erase=False, before=before, after=after, board_dir="",
                esptool=None,
            )
            patches = [
                mock.patch.object(firmware, "emit_result"),
                mock.patch.object(firmware, "emit_log"),
                mock.patch.object(firmware, "HOST", "linux"),
                mock.patch.object(firmware, "_esptool_cmd", return_value=["esptool"]),
                mock.patch.object(firmware, "_port_usb_id", return_value=usb_id),
            ]
            if layout is None:
                patches.append(
                    mock.patch.object(firmware, "_esp32_layout_check", return_value={})
                )
            with mock.patch.object(firmware, "stream_process", return_value=0) as run:
                for p in patches:
                    p.start()
                try:
                    firmware.flash_esp32(ns, None, fw)
                finally:
                    for p in patches:
                        p.stop()
            return [c[0][0] for c in run.call_args_list]

    def test_s2_rom_port_flashes_then_watchdog_resets(self):
        (cmd,) = self.run_flash(S2_ROM)
        self.assertEqual(cmd[cmd.index("--before") + 1], "no-reset")
        self.assertEqual(cmd[cmd.index("--after") + 1], "watchdog-reset")

    def test_explicit_reset_modes_win(self):
        (cmd,) = self.run_flash(S2_ROM, before="default-reset", after="no-reset")
        self.assertEqual(cmd[cmd.index("--before") + 1], "default-reset")
        self.assertEqual(cmd[cmd.index("--after") + 1], "no-reset")

    def test_other_ports_keep_hard_reset(self):
        (cmd,) = self.run_flash(MICROPYTHON_CDC)
        self.assertEqual(cmd[cmd.index("--before") + 1], "default-reset")
        self.assertEqual(cmd[cmd.index("--after") + 1], "hard-reset")

    def test_partition_table_read_leaves_s2_in_its_rom(self):
        seen = []

        def fake_run(cmd, **_kw):
            seen.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 1, "", "")

        with mock.patch.object(firmware, "HOST", "linux"), \
                mock.patch.object(firmware.subprocess, "run", side_effect=fake_run):
            got = firmware._read_device_partition_table(
                ["esptool", "-p", "COM29"], 0xC00, s2_rom_usb=True
            )
        self.assertIsNone(got)
        self.assertEqual(len(seen), 1, "a second try would reset the chip")
        self.assertEqual(resets_the_chip(seen[0]), [], seen[0])

        seen.clear()
        with mock.patch.object(firmware, "HOST", "linux"), \
                mock.patch.object(firmware.subprocess, "run", side_effect=fake_run):
            firmware._read_device_partition_table(["esptool", "-p", "COM4"], 0xC00)
        self.assertEqual(
            [c[c.index("--before") + 1] for c in seen], ["default-reset", "no-reset"]
        )


class PortIdTests(unittest.TestCase):
    def test_asks_the_python_that_runs_esptool(self):
        rows = [["COM30", 0x303A, 0x4001], ["COM29", 0x303A, 0x0002]]
        done = subprocess.CompletedProcess([], 0, json.dumps(rows), "")
        ns = argparse.Namespace(device="COM29", esptool=None)
        with mock.patch.object(firmware, "_esptool_cmd",
                               return_value=["python.exe", "-m", "esptool"]), \
                mock.patch.object(firmware.subprocess, "run", return_value=done) as run:
            self.assertEqual(firmware._port_usb_id(ns), S2_ROM)
            self.assertTrue(firmware._is_s2_rom_usb(ns))
        self.assertEqual(run.call_args[0][0][:2], ["python.exe", "-c"])

    def test_unknown_port_is_none(self):
        done = subprocess.CompletedProcess([], 0, "[]", "")
        ns = argparse.Namespace(device="COM29", esptool=None)
        with mock.patch.object(firmware, "_esptool_cmd",
                               return_value=["python.exe", "-m", "esptool"]), \
                mock.patch.object(firmware.subprocess, "run", return_value=done):
            self.assertIsNone(firmware._port_usb_id(ns))
            self.assertFalse(firmware._is_s2_rom_usb(ns))


if __name__ == "__main__":
    unittest.main()
