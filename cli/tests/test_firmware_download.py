"""The official firmware catalog (mpftp.firmware_download).

No network: micropython.org's board index is a trimmed copy in fixtures/, and
the board pages are a few lines of the real pages' links.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mpftp import firmware_download as fd

FIXTURES = Path(__file__).resolve().parent / "fixtures"

PAGES = {
    "ESP32_GENERIC_S3": """
        <a href="/resources/firmware/ESP32_GENERIC_S3-20260824-v1.29.0.uf2">.uf2</a>
        <a href="/resources/firmware/ESP32_GENERIC_S3-20260824-v1.29.0.app-bin">.app-bin</a>
        <a href="/resources/firmware/ESP32_GENERIC_S3-20260824-v1.29.0.bin">.bin</a>
        <a href="/resources/firmware/ESP32_GENERIC_S3-20260824-v1.29.0.meta.zip">.zip</a>
        <a href="/resources/firmware/ESP32_GENERIC_S3-20260406-v1.28.0.bin">.bin</a>
        <a href="/resources/firmware/ESP32_GENERIC_S3-20260929-v1.30.0-preview.95.g5b5f06cb48.bin">.bin</a>
        <a href="/resources/firmware/ESP32_GENERIC_S3-20260921-v1.30.0-preview.71.g1ea77a5d65.bin">.bin</a>
        <a href="/resources/firmware/ESP32_GENERIC_S3-SPIRAM_OCT-20260824-v1.29.0.bin">.bin</a>
        <a href="/resources/firmware/ESP32_GENERIC_S3-SPIRAM_OCT-20260824-v1.29.0.uf2">.uf2</a>
    """,
    "ESP32_GENERIC_P4": """
        <a href="/resources/firmware/ESP32_GENERIC_P4-20260406-v1.28.0.bin">bin</a>
        <a href="/resources/firmware/ESP32_GENERIC_P4-C6_WIFI-20260406-v1.28.0.bin">bin</a>
        <a href="/resources/firmware/ESP32_GENERIC_P4-C5_WIFI-20260406-v1.28.0.bin">bin</a>
        <a href="/resources/firmware/ESP32_GENERIC_P4-C6_WIFI-20260717-v1.29.0-preview.100.gabcdef0123.bin">bin</a>
    """,
    "RPI_PICO": """
        <a href="/resources/firmware/RPI_PICO-20260824-v1.29.0.uf2">.uf2</a>
        <a href="/resources/firmware/RPI_PICO-20260406-v1.28.0.uf2">.uf2</a>
        <a href="/resources/firmware/RPI_PICO-20180915-v1.9.4.uf2">.uf2</a>
        <a href="/resources/firmware/RPI_PICO-20260929-v1.30.0-preview.95.g5b5f06cb48.uf2">.uf2</a>
    """,
    "SEEED_XIAO_NRF52": """
        <a href="/resources/firmware/SEEED_XIAO_NRF52-20260824-v1.29.0.hex">.hex</a>
        <a href="/resources/firmware/SEEED_XIAO_NRF52-20260824-v1.29.0.uf2">.uf2</a>
    """,
    "ESP32_GENERIC": "<p>nothing yet</p>",
}


def fake_fetch(url: str, timeout: float = 30.0) -> str:
    if url == fd.MICROPYTHON_BOARD_INDEX:
        return (FIXTURES / "micropython-boards-index.json").read_text("utf-8")
    prefix = fd.MICROPYTHON_ORG + "/download/"
    if url.startswith(prefix):
        return PAGES[url[len(prefix):].strip("/")]
    if url.endswith("/ESP32_GENERIC_P4/board.json"):
        return json.dumps({"mcu": "esp32p4", "deploy_options": {"flash_offset": "0x2000"}})
    raise RuntimeError(f"unexpected fetch in a test: {url}")


class CacheIsolated(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        patcher = mock.patch.object(fd, "CACHE_ROOT", Path(td.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.catalog = fd.load_catalog(fetch=fake_fetch, force=True)

    def entry(self, board):
        e = fd.find_variant(board, catalog=self.catalog)
        assert e is not None, board
        return e


class CatalogTests(CacheIsolated):
    def test_the_index_becomes_boards_with_port_mcu_and_offset(self):
        p4 = self.entry("esp32_generic_p4")
        self.assertEqual(p4["port"], "esp32")
        self.assertEqual(p4["family"], "esp32p4")
        self.assertEqual(p4["flash_offset"], "0x2000")
        self.assertEqual(p4["source"], "micropython.org")
        self.assertEqual(p4["info_url"], "https://micropython.org/download/ESP32_GENERIC_P4/")
        self.assertIn("C6_WIFI", p4["variants"])
        self.assertEqual(self.entry("ESP32_GENERIC_S3")["flash_offset"], "0x0")

    def test_ports_mpftp_cannot_flash_are_left_out(self):
        boards = {b["board"] for b in self.catalog}
        self.assertNotIn("PYBV11", boards)  # stm32: .dfu and .hex
        self.assertNotIn("ESP8266_GENERIC", boards)
        self.assertIn("SEEED_XIAO_NRF52", boards)

    def test_the_tree_groups_by_port_with_each_ports_flasher(self):
        tree = fd.catalog_tree(self.catalog)
        flashers = {p["port"]: p["flasher"] for p in tree["ports"]}
        self.assertEqual(flashers, {"esp32": "esptool", "rp2": "uf2", "nrf": "uf2"})
        self.assertEqual([p["port"] for p in tree["ports"]], ["esp32", "rp2", "nrf"])
        pico = next(b for p in tree["ports"] for b in p["boards"] if b["board"] == "RPI_PICO")
        self.assertEqual(pico["label"], "Raspberry Pi Pico (RPI_PICO)")
        self.assertEqual(tree["sources"], ["micropython.org"])

    def test_the_index_is_cached(self):
        calls = []

        def counting(url, timeout=30.0):
            calls.append(url)
            return fake_fetch(url, timeout)

        fd.load_catalog(fetch=counting)
        self.assertEqual(calls, [])


class BoardPageTests(CacheIsolated):
    def test_files_are_parsed_by_variant_kind_and_channel(self):
        files = fd.parse_board_page("ESP32_GENERIC_S3", PAGES["ESP32_GENERIC_S3"])
        kinds = {f["kind"] for f in files}
        self.assertEqual(kinds, {"bin", "uf2", "app-bin", "meta.zip"})
        oct_ = [f for f in files if f["variant"] == "SPIRAM_OCT"]
        self.assertEqual(len(oct_), 2)
        preview = next(f for f in files if f["channel"] == "preview")
        self.assertEqual(preview["version"], "1.30.0-preview.95.g5b5f06cb48")
        self.assertEqual(preview["date"], "20260929")

    def test_releases_come_first_newest_first_with_numeric_versions(self):
        files = fd.board_files(self.entry("RPI_PICO"), fetch=fake_fetch)
        self.assertEqual(
            [f["version"] for f in files],
            ["1.29.0", "1.28.0", "1.9.4", "1.30.0-preview.95.g5b5f06cb48"],
        )

    def test_only_esp_bins_and_uf2s_are_flashable_and_bins_carry_the_offset(self):
        entry = self.entry("ESP32_GENERIC_P4")
        by_var = fd.downloads_by_variant(entry, fetch=fake_fetch)
        self.assertEqual(set(by_var), {"", "C6_WIFI", "C5_WIFI"})
        self.assertTrue(all(d["offset"] == "0x2000" for v in by_var.values() for d in v))
        nrf = fd.downloads_by_variant(self.entry("SEEED_XIAO_NRF52"), fetch=fake_fetch)
        self.assertEqual([d["kind"] for d in nrf[""]], ["uf2"])
        s3 = fd.downloads_by_variant(self.entry("ESP32_GENERIC_S3"), fetch=fake_fetch)
        self.assertNotIn("app-bin", {d["kind"] for d in s3[""]})


class PickTests(CacheIsolated):
    def test_esp32_gets_the_newest_release_bin(self):
        chosen = fd.pick_download(self.entry("ESP32_GENERIC_S3"), fetch=fake_fetch)
        self.assertTrue(chosen["url"].endswith("ESP32_GENERIC_S3-20260824-v1.29.0.bin"), chosen["url"])

    def test_uf2_true_gets_the_uf2_on_an_esp_board(self):
        chosen = fd.pick_download(self.entry("ESP32_GENERIC_S3"), uf2=True, fetch=fake_fetch)
        self.assertEqual(chosen["kind"], "uf2")

    def test_rp2_gets_a_uf2_and_preview_gets_the_newest_preview(self):
        entry = self.entry("RPI_PICO")
        self.assertTrue(fd.pick_download(entry, fetch=fake_fetch)["url"].endswith("v1.29.0.uf2"))
        preview = fd.pick_download(entry, preview=True, fetch=fake_fetch)
        self.assertEqual(preview["channel"], "preview")

    def test_a_pinned_version_and_a_variant(self):
        entry = self.entry("ESP32_GENERIC_P4")
        chosen = fd.pick_download(entry, mp_variant="C6_WIFI", version="v1.28.0", fetch=fake_fetch)
        self.assertIn("-C6_WIFI-20260406-v1.28.0.bin", chosen["url"])
        preview = fd.pick_download(entry, mp_variant="C6_WIFI", preview=True, fetch=fake_fetch)
        self.assertIn("C6_WIFI", preview["url"])

    def test_misses_say_what_there_is(self):
        entry = self.entry("ESP32_GENERIC_P4")
        with self.assertRaisesRegex(RuntimeError, "C5_WIFI, C6_WIFI"):
            fd.pick_download(entry, mp_variant="NOPE", fetch=fake_fetch)
        with self.assertRaisesRegex(RuntimeError, "version 9.9.9 not found"):
            fd.pick_download(entry, version="9.9.9", fetch=fake_fetch)
        with self.assertRaisesRegex(RuntimeError, "no preview build"):
            fd.pick_download(self.entry("ESP32_GENERIC_S3"), mp_variant="SPIRAM_OCT", preview=True, fetch=fake_fetch)


class ListBoardTests(CacheIsolated):
    def test_list_board_has_variants_downloads_and_offset(self):
        info = fd.list_board("ESP32_GENERIC_P4", mp_variant="C6_WIFI", catalog=self.catalog, fetch=fake_fetch)
        self.assertEqual(info["variants"], ["C5_WIFI", "C6_WIFI"])
        self.assertEqual(info["flashOffset"], "0x2000")
        self.assertEqual(info["flasher"], "esptool")
        self.assertTrue(all("C6_WIFI" in d["url"] for d in info["downloads"]))

    def test_an_unknown_board_says_so(self):
        with self.assertRaisesRegex(RuntimeError, "not in the download catalog"):
            fd.list_board("NOT_A_BOARD", catalog=self.catalog, fetch=fake_fetch)


class SourceTests(CacheIsolated):
    def test_the_catalog_shows_whatever_the_sources_return(self):
        class Other(fd.FirmwareSource):
            name = "other"

            def boards(self, *, fetch=None, force=False):
                return [{
                    "source": "other", "board": "RPI_PICO", "vendor": "X", "model": "Y", "title": "X Y",
                    "family": "rp2040", "port": "rp2", "info_url": "", "variants": {}, "flash_offset": "",
                    "features": [],
                }, {
                    "source": "other", "board": "OTHER_BOARD", "vendor": "", "model": "", "title": "",
                    "family": "rp2350", "port": "rp2", "info_url": "", "variants": {}, "flash_offset": "",
                    "features": [],
                }]

            def firmware(self, board, *, fetch=None, force=False):
                return [{
                    "name": "o.uf2", "version": "1.0.0", "date": "20260101", "url": "https://example.invalid/o.uf2",
                    "kind": "uf2", "variant": "", "channel": "release", "offset": "",
                }]

        with mock.patch.object(fd, "SOURCES", [fd.MicroPythonOrg(), Other()]):
            catalog = fd.load_catalog(fetch=fake_fetch)
            self.assertEqual(fd.find_variant("RPI_PICO", catalog=catalog)["source"], "micropython.org")
            other = fd.find_variant("OTHER_BOARD", catalog=catalog)
            self.assertEqual(fd.pick_download(other)["url"], "https://example.invalid/o.uf2")


class FlashOffsetTests(CacheIsolated):
    def test_board_json_offset(self):
        self.assertEqual(fd.normalize_flash_offset("0"), "0x0")
        self.assertEqual(fd.normalize_flash_offset("0x2000"), "0x2000")
        off = fd.resolve_remote_flash_offset("ESP32_GENERIC_P4", fetch=fake_fetch, force=True)
        self.assertEqual(off, "0x2000")


if __name__ == "__main__":
    unittest.main()
