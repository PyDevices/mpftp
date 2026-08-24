"""Unit tests for firmware_download (no network — fixtures + fake fetch)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mpftp.firmware_download import (
    board_id_from_info_url,
    catalog_tree,
    find_variant,
    list_board,
    list_downloads,
    load_catalog,
    maybe_latest_preview,
    normalize_flash_offset,
    normalize_variant,
    parse_board_page_firmware,
    pick_download,
    port_for_family,
    resolve_remote_flash_offset,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture_fetch(url: str, timeout: float = 30.0) -> str:
    if url.endswith("micropython-variants-esptool.json"):
        return (FIXTURES / "micropython-variants-esptool.json").read_text("utf-8")
    if url.endswith("micropython-variants-uf2.json"):
        return (FIXTURES / "micropython-variants-uf2.json").read_text("utf-8")
    if url.endswith("/ESP32_GENERIC_P4/board.json"):
        return json.dumps(
            {
                "mcu": "esp32p4",
                "deploy_options": {"flash_offset": "0x2000"},
            }
        )
    if url.endswith("/ESP32_GENERIC/board.json"):
        return json.dumps(
            {
                "mcu": "esp32",
                "deploy_options": {"flash_offset": "0x1000"},
            }
        )
    if "ESP32_GENERIC_P4" in url:
        return """
        <html><body>
        <a href="/resources/firmware/ESP32_GENERIC_P4-20260406-v1.28.0.bin">bin</a>
        <a href="/resources/firmware/ESP32_GENERIC_P4-C6_WIFI-20260406-v1.28.0.bin">bin</a>
        <a href="/resources/firmware/ESP32_GENERIC_P4-C5_WIFI-20260406-v1.28.0.bin">bin</a>
        <a href="/resources/firmware/ESP32_GENERIC_P4-C6_WIFI-20260717-v1.29.0-preview.100.gabcdef0123.bin">bin</a>
        </body></html>
        """
    if "ESP32_GENERIC" in url and "download" in url:
        return """
        <html><body>
        <a href="/resources/firmware/ESP32_GENERIC-20260717-v1.29.0-preview.590.g772df1ae57.bin">bin</a>
        </body></html>
        """
    raise RuntimeError(f"unexpected fetch url in test: {url}")


class NormalizeTests(unittest.TestCase):
    def test_board_id(self):
        self.assertEqual(
            board_id_from_info_url("https://micropython.org/download/ESP32_GENERIC"),
            "ESP32_GENERIC",
        )

    def test_port_for_family(self):
        self.assertEqual(port_for_family("esp32s3"), "esp32")
        self.assertEqual(port_for_family("rp2"), "rp2")
        self.assertEqual(port_for_family("samd21"), "samd")

    def test_normalize(self):
        raw = json.loads((FIXTURES / "micropython-variants-esptool.json").read_text())[1]
        v = normalize_variant(raw)
        self.assertEqual(v["board"], "ESP32_GENERIC")
        self.assertEqual(v["port"], "esp32")
        self.assertTrue(v["downloads"])


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.patcher = mock.patch("mpftp.firmware_download.CACHE_ROOT", Path(self._td.name))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_load_and_tree(self):
        cat = load_catalog(fetch=_fixture_fetch, force=True)
        self.assertTrue(any(v["board"] == "ESP32_GENERIC" for v in cat))
        self.assertTrue(any(v["port"] == "rp2" for v in cat))
        tree = catalog_tree(cat)
        ports = {p["port"] for p in tree["ports"]}
        self.assertIn("esp32", ports)
        self.assertIn("rp2", ports)

    def test_find_and_list(self):
        cat = load_catalog(fetch=_fixture_fetch, force=True)
        v = find_variant("ESP32_GENERIC", catalog=cat)
        self.assertIsNotNone(v)
        assert v is not None
        dls = list_downloads(v)
        self.assertTrue(any(d["channel"] == "release" for d in dls))
        info = list_board("ESP32_GENERIC", catalog=cat, fetch=_fixture_fetch)
        self.assertEqual(info["board"], "ESP32_GENERIC")

    def test_pick_latest_release(self):
        cat = load_catalog(fetch=_fixture_fetch, force=True)
        v = find_variant("ESP32_GENERIC", catalog=cat)
        assert v is not None
        chosen = pick_download(v, fetch=_fixture_fetch)
        self.assertEqual(chosen["channel"], "release")
        self.assertIn("1.28.0", chosen["version"])

    def test_preview_from_page(self):
        cat = load_catalog(fetch=_fixture_fetch, force=True)
        v = find_variant("ESP32_GENERIC", catalog=cat)
        assert v is not None
        # Inject regex like Thonny does for some boards.
        v = dict(v)
        v["latest_prerelease_regex"] = r"\d{8}-v1\.29\.0-preview\.\d+\.[a-z0-9]{10}"
        prev = maybe_latest_preview(v, fetch=_fixture_fetch)
        self.assertIsNotNone(prev)
        assert prev is not None
        self.assertEqual(prev["channel"], "preview")
        self.assertIn("preview", prev["url"])

    def test_p4_c6_wifi_variant(self):
        cat = load_catalog(fetch=_fixture_fetch, force=True)
        v = find_variant("ESP32_GENERIC_P4", catalog=cat)
        assert v is not None
        info = list_board(
            "ESP32_GENERIC_P4",
            mp_variant="C6_WIFI",
            catalog=cat,
            fetch=_fixture_fetch,
        )
        self.assertIn("C6_WIFI", info["variants"])
        self.assertIn("C5_WIFI", info["variants"])
        self.assertTrue(info["downloads"])
        self.assertTrue(
            all("C6_WIFI" in d["url"] for d in info["downloads"])
        )
        chosen = pick_download(v, mp_variant="C6_WIFI", fetch=_fixture_fetch)
        self.assertEqual(chosen["channel"], "release")
        self.assertIn("C6_WIFI", chosen["url"])
        self.assertNotIn("C5_WIFI", chosen["url"])
        preview = pick_download(
            v, mp_variant="C6_WIFI", preview=True, fetch=_fixture_fetch
        )
        self.assertEqual(preview["channel"], "preview")
        self.assertIn("C6_WIFI", preview["url"])

    def test_parse_board_page_groups_variants(self):
        html = _fixture_fetch("https://micropython.org/download/ESP32_GENERIC_P4/")
        grouped = parse_board_page_firmware("ESP32_GENERIC_P4", html)
        self.assertIn("", grouped)
        self.assertIn("C6_WIFI", grouped)
        self.assertIn("C5_WIFI", grouped)

    def test_remote_board_json_flash_offset(self):
        self.assertEqual(normalize_flash_offset("0"), "0x0")
        self.assertEqual(normalize_flash_offset("0x2000"), "0x2000")
        off = resolve_remote_flash_offset(
            "ESP32_GENERIC_P4", port="esp32", fetch=_fixture_fetch, force=True
        )
        self.assertEqual(off, "0x2000")
        info = list_board(
            "ESP32_GENERIC_P4",
            catalog=load_catalog(fetch=_fixture_fetch, force=True),
            fetch=_fixture_fetch,
        )
        self.assertEqual(info.get("flashOffset"), "0x2000")


if __name__ == "__main__":
    unittest.main()
