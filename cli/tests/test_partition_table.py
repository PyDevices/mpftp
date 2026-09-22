"""Binary partition tables: decode, diff, and find one inside a firmware image.

The check these back exists because a T-Embed S3 was flashed through its ROM
download port with an image whose ``vfs`` sat at 0x390000 where every image that
board had run has it at 0x3a0000. esptool verified the write; the board then
never enumerated, because MicroPython's ``_boot.py`` found a first block that
was neither blank nor mountable and sat in ``inisetup.fs_corrupted()`` before
the runtime USB device starts. No panic, no core dump, nothing on the bus.

So each test here plants the fault it is meant to catch, rather than asserting
that a good table looks good -- a comparison that cannot fail is not evidence.
"""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from mpftp.firmware import (
    _PARTITION_TABLE_OFFSET,
    diff_partition_tables,
    parse_partition_table,
    partition_table_from_image,
)


def entry(name: str, ptype: int, subtype: int, offset: int, size: int) -> bytes:
    return (
        b"\xaa\x50"
        + bytes([ptype, subtype])
        + struct.pack("<II", offset, size)
        + name.encode().ljust(16, b"\x00")
        + struct.pack("<I", 0)
    )


# The layout the S3 image really carries, read off firmware-s3-bounce.bin.
GOOD = (
    entry("nvs", 1, 0x02, 0x9000, 0x6000)
    + entry("phy_init", 1, 0x01, 0xF000, 0x1000)
    + entry("factory", 0, 0x00, 0x10000, 0x380000)
    + entry("coredump", 1, 0x03, 0x390000, 0x10000)
    + entry("vfs", 1, 0x81, 0x3A0000, 0x60000)
)
# The T-Embed's fault: vfs 64 KB low, on top of where the core dump lives.
MOVED_VFS = (
    entry("nvs", 1, 0x02, 0x9000, 0x6000)
    + entry("phy_init", 1, 0x01, 0xF000, 0x1000)
    + entry("factory", 0, 0x00, 0x10000, 0x380000)
    + entry("coredump", 1, 0x03, 0x390000, 0x10000)
    + entry("vfs", 1, 0x81, 0x390000, 0x60000)
)


class ParseTests(unittest.TestCase):
    def test_it_decodes_names_offsets_and_subtypes(self):
        rows = parse_partition_table(GOOD)
        self.assertEqual(
            ["nvs", "phy_init", "factory", "coredump", "vfs"],
            [r["name"] for r in rows],
        )
        vfs = rows[-1]
        self.assertEqual(("data", "fat", 0x3A0000, 0x60000),
                         (vfs["type"], vfs["subtype"], vfs["offset"], vfs["size"]))

    def test_it_stops_at_the_checksum_entry(self):
        blob = GOOD + b"\xeb\xeb" + b"\x00" * 30 + b"\xff" * 64
        self.assertEqual(5, len(parse_partition_table(blob)))

    def test_it_stops_at_erase_padding(self):
        self.assertEqual(5, len(parse_partition_table(GOOD + b"\xff" * 256)))

    def test_a_region_that_is_not_a_table_decodes_to_nothing(self):
        self.assertEqual([], parse_partition_table(b"\xff" * 0xC00))


class DiffTests(unittest.TestCase):
    def test_identical_tables_produce_no_differences(self):
        # The control. Without it, a differ that always complains would pass
        # every other test in this class.
        rows = parse_partition_table(GOOD)
        self.assertEqual([], diff_partition_tables(rows, rows))

    def test_it_names_the_partition_the_field_and_both_values(self):
        got = diff_partition_tables(
            parse_partition_table(GOOD), parse_partition_table(MOVED_VFS)
        )
        self.assertEqual(1, len(got), got)
        self.assertIn("vfs", got[0])
        self.assertIn("offset", got[0])
        self.assertIn(hex(0x390000), got[0])   # what the device has
        self.assertIn(hex(0x3A0000), got[0])   # what the image wants

    def test_it_reports_a_partition_the_device_does_not_have(self):
        shorter = parse_partition_table(GOOD[: 4 * 32])
        got = diff_partition_tables(parse_partition_table(GOOD), shorter)
        self.assertTrue(any("vfs" in d and "absent on the device" in d for d in got), got)

    def test_it_reports_a_partition_the_image_does_not_have(self):
        shorter = parse_partition_table(GOOD[: 4 * 32])
        got = diff_partition_tables(shorter, parse_partition_table(GOOD))
        self.assertTrue(any("vfs" in d and "absent from the image" in d for d in got), got)

    def test_it_reports_a_resized_partition(self):
        resized = GOOD[: 4 * 32] + entry("vfs", 1, 0x81, 0x3A0000, 0x50000)
        got = diff_partition_tables(
            parse_partition_table(GOOD), parse_partition_table(resized)
        )
        self.assertTrue(any("vfs" in d and "size" in d for d in got), got)


class FromImageTests(unittest.TestCase):
    def _image(self, table: bytes) -> Path:
        tmp = Path(tempfile.mkdtemp()) / "firmware.bin"
        blob = bytearray(b"\x00" * (_PARTITION_TABLE_OFFSET + 0xC00))
        blob[_PARTITION_TABLE_OFFSET : _PARTITION_TABLE_OFFSET + len(table)] = table
        for i in range(_PARTITION_TABLE_OFFSET + len(table), len(blob)):
            blob[i] = 0xFF
        tmp.write_bytes(bytes(blob))
        return tmp

    def test_it_finds_the_table_a_whole_flash_image_carries(self):
        # This is the case that used to skip the check entirely: a saved .bin
        # flashed with --artifact has no sibling partition_table/ directory.
        rows = parse_partition_table(partition_table_from_image(self._image(GOOD), 0))
        self.assertEqual("vfs", rows[-1]["name"])

    def test_it_declines_a_partial_image(self):
        # Written at a non-zero offset, the file is not a whole-flash image and
        # 0x8000 into it is application code, not a table.
        self.assertIsNone(partition_table_from_image(self._image(GOOD), 0x10000))

    def test_it_declines_a_file_with_no_table_where_one_should_be(self):
        tmp = Path(tempfile.mkdtemp()) / "notfirmware.bin"
        tmp.write_bytes(b"\x00" * (_PARTITION_TABLE_OFFSET + 0xC00))
        self.assertIsNone(partition_table_from_image(tmp, 0))

    def test_it_declines_a_file_too_short_to_reach_the_table(self):
        tmp = Path(tempfile.mkdtemp()) / "short.bin"
        tmp.write_bytes(b"\x00" * 16)
        self.assertIsNone(partition_table_from_image(tmp, 0))


if __name__ == "__main__":
    unittest.main()
