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


if __name__ == "__main__":
    unittest.main()
