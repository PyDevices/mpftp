"""Unit tests for the firmware Detect parsers / matcher (no hardware).

Sample esptool text is taken from the fixture campaign (ESP32-P4, ESP32-S3
with/without PSRAM, classic ESP32-PICO-V3-02) captured in the plan.
"""

from __future__ import annotations

import unittest
from pathlib import Path


def _load_engine():
    from mpftp import firmware

    return firmware


# Fixture 1 — ESP32-P4 32 MB (COM4), esptool-only.
P4_FLASH_ID = """\
esptool.py v4.7.0
Serial port COM4
Connecting....
Chip is ESP32-P4 (revision v1.3)
Features: Dual Core + LP Core, 400MHz
Crystal is 40MHz
MAC: e8:f6:0a:e0:f0:70
Uploading stub...
Running stub...
Stub running...
Manufacturer: 20
Device: 4020
Detected flash size: 32MB
Hard resetting via RTS pin...
"""

# Fixture 1b — same ESP32-P4, captured verbatim from esptool v5.3.1 (COM4 CH343
# bridge). v5 renamed fields: "Chip type:" (was "Chip is"), "Crystal frequency:"
# (was "Crystal is"). The v4 parser silently reported "not an ESP" on this.
P4_FLASH_ID_V5 = """\
Warning: Deprecated: Command 'flash_id' is deprecated. Use 'flash-id' instead.
esptool v5.3.1
Serial port COM4:
Connecting....
Detecting chip type... ESP32-P4
Connected to ESP32-P4 on COM4:
Chip type:          ESP32-P4 (revision v1.3)
Features:           Dual Core + LP Core, 400MHz
Crystal frequency:  40MHz
MAC:                e8:f6:0a:e0:f0:70

Uploading stub flasher...
Running stub flasher...
Stub flasher running.

Flash Memory Information:
=========================
Manufacturer: c8
Device: 4019
Detected flash size: 32MB

Hard resetting via RTS pin...
"""

SECURITY_OFF = """\
Security Information:
=====================
Flags: 0x00000000 (0b0)
Secure Boot: Disabled
Flash Encryption: Disabled
"""

# Fixture 2 — ESP32-S3, embedded flash, no PSRAM.
S3_NO_PSRAM = """\
Chip is ESP32-S3 (QFN56) (revision v0.1)
Features: Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz, Embedded Flash 8MB (GD)
Crystal is 40MHz
MAC: f4:12:fa:8d:95:cc
Detected flash size: 8MB
"""

# Fixture 6 — ESP32-S3 with embedded PSRAM (no MicroPython).
S3_PSRAM = """\
Chip is ESP32-S3 (QFN56) (revision v0.2)
Features: Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz, Embedded PSRAM 8MB (AP_3v3)
Crystal is 40MHz
MAC: 30:30:f9:0f:6a:a4
Detected flash size: 8MB
"""

# Fixture 17 — classic ESP32-PICO-V3-02 with embedded PSRAM.
PICO_V3 = """\
Chip is ESP32-PICO-V3-02 (revision v3.0)
Features: Wi-Fi, BT, Dual Core + LP Core, 240MHz, Embedded Flash, Embedded PSRAM, Vref calibration in eFuse
Crystal is 40MHz
MAC: e8:9f:6d:2e:ec:94
Detected flash size: 8MB
"""

# Non-ESP: esptool cannot talk to the ROM bootloader.
NOT_ESP = """\
esptool.py v4.7.0
Serial port COM14
Connecting......
A fatal error occurred: Failed to connect to Espressif device: No serial data received.
"""


def _tree():
    return [
        {
            "port": "esp32",
            "kind": "boards",
            "boards": [
                {"board": "ESP32_GENERIC", "variants": ["SPIRAM", "D2WD", "UNICORE"]},
                {"board": "ESP32_GENERIC_S3", "variants": ["SPIRAM", "SPIRAM_OCT", "FLASH_4M"]},
                {"board": "ESP32_GENERIC_P4", "variants": ["C5_WIFI", "C6_WIFI"]},
            ],
        }
    ]


class ParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = _load_engine()

    def test_parse_p4(self):
        f = self.eng.parse_esptool_flash_id(P4_FLASH_ID)
        self.assertEqual(f["chip"], "ESP32-P4")
        self.assertEqual(f["revision"], "v1.3")
        self.assertEqual(f["cores"], 2)
        self.assertTrue(f["lpCore"])
        self.assertEqual(f["maxMhz"], 400)
        self.assertEqual(f["crystalMhz"], 40)
        self.assertEqual(f["mac"], "e8:f6:0a:e0:f0:70")
        self.assertEqual(f["flashMb"], 32)
        self.assertFalse(f["psram"]["present"])

    def test_parse_p4_esptool_v5(self):
        f = self.eng.parse_esptool_flash_id(P4_FLASH_ID_V5)
        self.assertEqual(f["chip"], "ESP32-P4")
        self.assertEqual(f["revision"], "v1.3")
        self.assertEqual(f["cores"], 2)
        self.assertTrue(f["lpCore"])
        self.assertEqual(f["maxMhz"], 400)
        self.assertEqual(f["crystalMhz"], 40)
        self.assertEqual(f["mac"], "e8:f6:0a:e0:f0:70")
        self.assertEqual(f["flashMb"], 32)

    def test_parse_s3_qfn_and_flash(self):
        f = self.eng.parse_esptool_flash_id(S3_NO_PSRAM)
        self.assertEqual(f["chip"], "ESP32-S3")
        self.assertEqual(f["revision"], "v0.1")
        self.assertEqual(f["maxMhz"], 240)
        self.assertEqual(f["flashMb"], 8)
        self.assertFalse(f["psram"]["present"])

    def test_parse_s3_embedded_psram(self):
        f = self.eng.parse_esptool_flash_id(S3_PSRAM)
        self.assertTrue(f["psram"]["present"])
        self.assertFalse(f["psram"]["octal"])

    def test_parse_security(self):
        s = self.eng.parse_esptool_security(SECURITY_OFF)
        self.assertTrue(s["available"])
        self.assertEqual(s["secureBoot"], "Disabled")
        self.assertEqual(s["flashEncryption"], "Disabled")

    def test_security_absent(self):
        s = self.eng.parse_esptool_security("get_security_info is not implemented")
        self.assertFalse(s["available"])

    def test_family_from_chip(self):
        self.assertEqual(self.eng.family_from_chip("ESP32-P4"), "P4")
        self.assertEqual(self.eng.family_from_chip("ESP32-S3"), "S3")
        self.assertEqual(self.eng.family_from_chip("ESP32-PICO-V3-02"), "")
        self.assertEqual(self.eng.family_from_chip("ESP32"), "")


class MatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = _load_engine()

    def _match(self, text, mp_hints=None):
        f = self.eng.parse_esptool_flash_id(text)
        family = self.eng.family_from_chip(f["chip"])
        return self.eng.match_esp_target(
            family, f["psram"], f["flashMb"], mp_hints or {}, _tree()
        )

    def test_p4_matched_default_variant(self):
        m = self._match(P4_FLASH_ID)
        self.assertEqual(m["board"], "ESP32_GENERIC_P4")
        self.assertEqual(m["variant"], "")
        self.assertEqual(m["flashSize"], "32MB")
        self.assertEqual(m["flashConfig"], "CONFIG_ESPTOOLPY_FLASHSIZE_32MB")
        self.assertEqual(m["confidence"], "matched")
        self.assertIn("C6_WIFI", m["variantOptions"])

    def test_p4_v5_matched(self):
        m = self._match(P4_FLASH_ID_V5)
        self.assertEqual(m["board"], "ESP32_GENERIC_P4")
        self.assertEqual(m["flashSize"], "32MB")
        self.assertEqual(m["confidence"], "matched")

    def test_p4_c6_wifi_from_mp_build(self):
        m = self._match(P4_FLASH_ID, {"build": "ESP32_GENERIC_P4-C6_WIFI"})
        self.assertEqual(m["variant"], "C6_WIFI")

    def test_p4_c6_wifi_from_hints_without_tree_variants(self):
        # Download mode: catalog boards have no variants until the page scrape.
        f = self.eng.parse_esptool_flash_id(P4_FLASH_ID)
        family = self.eng.family_from_chip(f["chip"])
        tree = [{"port": "esp32", "boards": [{"board": "ESP32_GENERIC_P4", "variants": []}]}]
        m = self.eng.match_esp_target(
            family,
            f["psram"],
            f["flashMb"],
            {"build": "ESP32_GENERIC_P4-C6_WIFI", "machine": "ESP32-P4"},
            tree,
        )
        self.assertEqual(m["variant"], "C6_WIFI")
        self.assertIn("C6_WIFI", m["variantOptions"])

    def test_s3_no_psram_default(self):
        m = self._match(S3_NO_PSRAM)
        self.assertEqual(m["board"], "ESP32_GENERIC_S3")
        self.assertEqual(m["variant"], "")
        self.assertEqual(m["flashSize"], "8MB")

    def test_s3_embedded_psram_spiram(self):
        m = self._match(S3_PSRAM)
        self.assertEqual(m["board"], "ESP32_GENERIC_S3")
        self.assertEqual(m["variant"], "SPIRAM")

    def test_s3_octal_from_machine_string(self):
        m = self._match(S3_NO_PSRAM, {"machine": "Generic ESP32S3 module with Octal-SPIRAM"})
        self.assertEqual(m["variant"], "SPIRAM_OCT")

    def test_s3_large_heap_spiram(self):
        m = self._match(S3_NO_PSRAM, {"memfree": 8_300_000})
        self.assertEqual(m["variant"], "SPIRAM")

    def test_pico_classic_spiram(self):
        m = self._match(PICO_V3)
        self.assertEqual(m["board"], "ESP32_GENERIC")
        self.assertEqual(m["variant"], "SPIRAM")

    def test_family_only_when_board_absent(self):
        f = self.eng.parse_esptool_flash_id(S3_NO_PSRAM)
        m = self.eng.match_esp_target("S3", f["psram"], 8, {}, [])
        self.assertEqual(m["confidence"], "family-only")
        self.assertEqual(m["board"], "ESP32_GENERIC_S3")


class ClassifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = _load_engine()

    def test_not_espressif_reason(self):
        self.assertIn("No serial data", self.eng._esptool_fail_reason(NOT_ESP, 2))

    def test_suggested_port_from_mp(self):
        self.assertEqual(self.eng.suggested_port_from_mp({"platform": "rp2"}), "rp2")
        self.assertEqual(self.eng.suggested_port_from_mp({"platform": "pyboard"}), "stm32")
        self.assertEqual(
            self.eng.suggested_port_from_mp({"platform": "nRF52840", "machine": "XIAO"}), "nrf"
        )
        self.assertEqual(
            self.eng.suggested_port_from_mp({"platform": "MicroChip SAMD51"}), "samd"
        )
        self.assertEqual(
            self.eng.suggested_port_from_mp({"platform": "NXP IMXRT10XX"}), "mimxrt"
        )

    def test_mp_indicates_esp(self):
        self.assertTrue(self.eng._mp_indicates_esp({"platform": "esp32"}))
        self.assertTrue(self.eng._mp_indicates_esp({"platform": "Espressif"}))
        self.assertFalse(self.eng._mp_indicates_esp({"platform": "rp2"}))


class SplitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = _load_engine()

    def _rows(self, text):
        return self.eng.parse_partitions_csv(text)

    def test_resize_existing_storage(self):
        base = self._rows(
            "# Name, Type, SubType, Offset, Size, Flags\n"
            "nvs, data, nvs, 0x9000, 0x6000,\n"
            "phy_init, data, phy, 0xf000, 0x1000,\n"
            "factory, app, factory, 0x10000, 0x200000,\n"
            "vfs, data, fat, 0x210000, 0x1F0000,\n"
        )
        rows, warnings = self.eng.compute_split(base, 0x400000, 8 * 1024 * 1024)
        storage = rows[-1]
        self.assertEqual(storage["name"], "vfs")
        self.assertEqual(self.eng._parse_size(storage["size"]), 0x400000)
        # factory ends at 0x210000, storage must start there.
        self.assertEqual(self.eng._parse_size(storage["offset"]), 0x210000)

    def test_append_when_only_factory(self):
        base = self._rows(
            "# Name, Type, SubType, Offset, Size, Flags\n"
            "nvs, data, nvs, 0x9000, 0x6000,\n"
            "phy_init, data, phy, 0xf000, 0x1000,\n"
            "factory, app, factory, 0x10000, 0x400000,\n"
        )
        rows, warnings = self.eng.compute_split(base, 0, 32 * 1024 * 1024)
        self.assertTrue(any("added a trailing vfs" in w for w in warnings))
        storage = rows[-1]
        self.assertTrue(self.eng._is_storage_row(storage))
        # storage fills to end of 32 MB flash.
        end = self.eng._table_target_size(rows)
        self.assertLessEqual(end, 32 * 1024 * 1024)
        self.assertGreater(self.eng._parse_size(storage["size"]), 0x1000000)

    def test_overflow_warning(self):
        base = self._rows(
            "# Name, Type, SubType, Offset, Size, Flags\n"
            "factory, app, factory, 0x10000, 0x200000,\n"
            "vfs, data, fat, 0x210000, 0x1F0000,\n"
        )
        rows, warnings = self.eng.compute_split(base, 32 * 1024 * 1024, 8 * 1024 * 1024)
        self.assertTrue(any("exceeds flash" in w for w in warnings))


# Real ESP-IDF check_sizes.py failure captured from an ESP32-P4 build.
OVERFLOW_LOG = """\
[1963/1963] cd .../esp-idf/esptool_py && python .../check_sizes.py ...
FAILED: esp-idf/esptool_py/CMakeFiles/app_check_size ...
Error: app partition is too small for binary micropython.bin size 0x2a4e60:
  - Part 'factory' 0/0 @ 0x10000 size 0x1f0000 (overflow 0xb4e60)
ninja: build stopped: subcommand failed.
"""


class AutosizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = _load_engine()

    def test_parse_overflow(self):
        info = self.eng.parse_partition_overflow(OVERFLOW_LOG)
        self.assertIsNotNone(info)
        self.assertEqual(info["imageSize"], 0x2A4E60)
        self.assertEqual(info["partName"], "factory")
        self.assertEqual(info["partOffset"], 0x10000)
        self.assertEqual(info["partSize"], 0x1F0000)
        self.assertEqual(info["overflow"], 0xB4E60)

    def test_parse_overflow_absent(self):
        self.assertIsNone(self.eng.parse_partition_overflow("Project build complete."))

    def test_autosize_size_aligned_with_headroom(self):
        # 0x2a4e60 -> align 0x2b0000 + 0x40000 headroom -> 0x2f0000.
        new = self.eng.autosize_app_partition_size(0x2A4E60)
        self.assertEqual(new % 0x10000, 0)
        self.assertGreaterEqual(new, 0x2A4E60)
        self.assertEqual(new, 0x2F0000)

    def test_resize_factory_only_table(self):
        # P4-style factory-only table: grow factory, no vfs to reflow.
        rows = self.eng.parse_partitions_csv(
            "# Name, Type, SubType, Offset, Size, Flags\n"
            "nvs, data, nvs, 0x9000, 0x6000,\n"
            "phy_init, data, phy, 0xf000, 0x1000,\n"
            "factory, app, factory, 0x10000, 0x1F0000,\n"
        )
        out = self.eng.resize_app_partition(rows, "factory", 0x300000)
        factory = next(r for r in out if r["name"] == "factory")
        self.assertEqual(self.eng._parse_size(factory["size"]), 0x300000)
        self.assertEqual(self.eng._parse_size(factory["offset"]), 0x10000)

    def test_resize_reflows_trailing_vfs(self):
        rows = self.eng.parse_partitions_csv(
            "# Name, Type, SubType, Offset, Size, Flags\n"
            "factory, app, factory, 0x10000, 0x1F0000,\n"
            "vfs, data, fat, 0x200000, 0x600000,\n"
        )
        out = self.eng.resize_app_partition(rows, "factory", 0x300000)
        vfs = next(r for r in out if r["name"] == "vfs")
        # vfs pushed to factory offset + new size.
        self.assertEqual(self.eng._parse_size(vfs["offset"]), 0x10000 + 0x300000)

    def test_resize_no_app_returns_none(self):
        rows = self.eng.parse_partitions_csv(
            "# Name, Type, SubType, Offset, Size, Flags\n"
            "nvs, data, nvs, 0x9000, 0x6000,\n"
        )
        self.assertIsNone(self.eng.resize_app_partition(rows, "factory", 0x300000))


if __name__ == "__main__":
    unittest.main()
