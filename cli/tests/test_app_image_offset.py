"""An application image written at the bootloader offset boot-loops the board.

mpftp#67: ``micropython.bin`` flashed with ``--artifact`` on the P4 went to
0x2000, the ROM loaded it as a bootloader, and the board sat in a watchdog
reset loop with its partition table overwritten. The combined
``firmware.bin`` is what belongs there. Each test plants the image that
caused it, not only a good one.
"""

from __future__ import annotations

import argparse
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mpftp import firmware


def image(desc_word: int) -> bytes:
    """An ESP image header, one segment header, then a 4-byte word at byte 32."""
    header = bytes([0xE9, 1, 2, 0x20]) + b"\x00" * 20
    segment = struct.pack("<II", 0x40280020, 0x100)
    return header + segment + struct.pack("<I", desc_word) + b"\x00" * 60


APP = image(0xABCD5432)  # esp_app_desc_t: what micropython.bin starts with
BOOTLOADER = image(0x50)  # esp_bootloader_desc_t: what firmware.bin starts with


class ImageKindTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name: str, data: bytes) -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path

    def test_an_app_image_is_recognised(self):
        self.assertTrue(firmware.esp_image_is_app(self.write("micropython.bin", APP)))

    def test_a_bootloader_led_image_is_not_an_app(self):
        self.assertFalse(firmware.esp_image_is_app(self.write("firmware.bin", BOOTLOADER)))

    def test_a_file_that_is_not_an_esp_image_is_not_an_app(self):
        self.assertFalse(firmware.esp_image_is_app(self.write("x.bin", b"\x00" * 64)))
        self.assertFalse(firmware.esp_image_is_app(self.write("short.bin", APP[:20])))

    def test_app_at_the_bootloader_offset_is_refused_and_names_firmware_bin(self):
        app = self.write("micropython.bin", APP)
        self.write("firmware.bin", BOOTLOADER)
        for offset in ("0x2000", "0x1000", "0x0"):
            with self.subTest(offset=offset):
                why = firmware.app_image_at_bootloader_error(app, offset)
                self.assertIsNotNone(why)
                self.assertIn(str(self.dir / "firmware.bin"), why)

    def test_app_at_the_app_partition_is_allowed(self):
        app = self.write("micropython.bin", APP)
        self.assertIsNone(firmware.app_image_at_bootloader_error(app, "0x10000"))

    def test_combined_image_at_the_bootloader_offset_is_allowed(self):
        fw = self.write("firmware.bin", BOOTLOADER)
        self.assertIsNone(firmware.app_image_at_bootloader_error(fw, "0x2000"))


class FlashEsp32Tests(unittest.TestCase):
    def test_flash_refuses_before_touching_the_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "micropython.bin"
            app.write_bytes(APP)
            ns = argparse.Namespace(
                port="esp32", board="", family="esp32p4", offset="", device="COM4",
                baud=460800, erase=False, before="", after="", board_dir="",
            )
            with mock.patch.object(firmware, "emit_result") as result, \
                    mock.patch.object(firmware, "emit_log"), \
                    mock.patch.object(firmware, "_esptool_cmd", return_value=["esptool"]), \
                    mock.patch.object(firmware, "_esp32_layout_check") as layout, \
                    mock.patch.object(firmware, "stream_process") as run:
                firmware.flash_esp32(ns, None, app)
            run.assert_not_called()
            layout.assert_not_called()
            args, kwargs = result.call_args
            self.assertFalse(args[0])
            self.assertIn("application image", kwargs["error"])



def combined(chip_id: int) -> bytes:
    """A combined firmware.bin's first header, built for one chip."""
    header = bytearray(BOOTLOADER)
    header[12:14] = chip_id.to_bytes(2, "little")
    return bytes(header)


class OffsetFromImageTests(unittest.TestCase):
    """2026-10-05: a P4 firmware.bin flashed with --artifact and no --board
    went to 0x0, not 0x2000, and the DEV-KIT boot-looped. The image names
    its chip, so the offset follows from it."""

    def test_the_image_names_its_chip(self):
        with tempfile.TemporaryDirectory() as tmp:
            for chip_id, mcu in ((0x12, "esp32p4"), (0x09, "esp32s3"), (0x00, "esp32")):
                fw = Path(tmp) / f"{mcu}.bin"
                fw.write_bytes(combined(chip_id))
                self.assertEqual(firmware.esp32_image_family(fw), mcu)
            junk = Path(tmp) / "junk.bin"
            junk.write_bytes(b"\x00" * 64)
            self.assertEqual(firmware.esp32_image_family(junk), "")

    def test_a_p4_image_with_no_board_goes_to_0x2000(self):
        with tempfile.TemporaryDirectory() as tmp:
            fw = Path(tmp) / "firmware.bin"
            fw.write_bytes(combined(0x12))
            ns = argparse.Namespace(
                port="esp32", board="", family="", offset="", device="COM31",
                baud=460800, erase=False, before="", after="", board_dir="",
            )
            with mock.patch.object(firmware, "emit_result"), \
                    mock.patch.object(firmware, "emit_log") as log, \
                    mock.patch.object(firmware, "_esptool_cmd", return_value=["esptool"]), \
                    mock.patch.object(firmware, "_esp32_layout_check", return_value={}), \
                    mock.patch.object(firmware, "stream_process", return_value=0):
                firmware.flash_esp32(ns, None, fw)
            self.assertIn(mock.call("[mpftp] flash offset 0x2000"), log.call_args_list)


class FlashCommandTests(unittest.TestCase):
    """What flash_esp32 hands esptool for a combined image."""

    def run_flash(self, chip_id: int, erase: bool) -> list[list[str]]:
        with tempfile.TemporaryDirectory() as tmp:
            fw = Path(tmp) / "firmware.bin"
            fw.write_bytes(combined(chip_id))
            ns = argparse.Namespace(
                port="esp32", board="", family="", offset="", device="COM31",
                baud=460800, erase=erase, before="", after="", board_dir="",
            )
            with mock.patch.object(firmware, "emit_result"), \
                    mock.patch.object(firmware, "emit_log"), \
                    mock.patch.object(firmware, "HOST", "linux"), \
                    mock.patch.object(firmware, "_esptool_cmd", return_value=["esptool"]), \
                    mock.patch.object(firmware, "_esp32_layout_check", return_value={}), \
                    mock.patch.object(firmware, "stream_process", return_value=0) as run:
                firmware.flash_esp32(ns, None, fw)
            return [c[0][0] for c in run.call_args_list]

    def test_the_chip_the_image_names_goes_to_esptool(self):
        (cmd,) = self.run_flash(0x09, erase=False)
        self.assertEqual(cmd[cmd.index("--chip") + 1], "esp32s3")

    def test_erase_is_one_write_flash_with_erase_all(self):
        # Two esptool runs (erase-flash, then write-flash) have to reset the
        # board into its ROM loader twice; one run does it once.
        cmds = self.run_flash(0x17, erase=True)
        self.assertEqual(len(cmds), 1, cmds)
        cmd = cmds[0]
        self.assertNotIn("erase-flash", cmd)
        tail = cmd[cmd.index("write-flash"):]
        self.assertEqual(tail[1:3], ["--erase-all", "0x2000"])  # the C5 keeps 0x0-0x2000 too


if __name__ == "__main__":
    unittest.main()
