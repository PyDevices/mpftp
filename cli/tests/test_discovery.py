"""Firmware path discovery: MicroPython + generic SDK trees."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mpftp.firmware import (
    discover_cmods,
    find_emsdk,
    find_idf,
    find_micropython,
    find_sdk_tree,
)


def _touch_mp(root: Path) -> Path:
    (root / "ports").mkdir(parents=True)
    (root / "py").mkdir(parents=True)
    return root


class TestFindMicropython(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        # Isolate from the developer's real ~/micropython, env, and saved state.
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("MP_DIR", None)
        self.home = mock.patch("mpftp.firmware.HOME", self.base / "home")
        self.home.start()
        (self.base / "home").mkdir(parents=True, exist_ok=True)
        self.state = mock.patch("mpftp.firmware.load_state", return_value={})
        self.state.start()

    def tearDown(self) -> None:
        self.state.stop()
        self.home.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_hint(self) -> None:
        mp = _touch_mp(self.base / "mp")
        self.assertEqual(find_micropython(str(mp)), mp.resolve())

    def test_mp_dir_env(self) -> None:
        mp = _touch_mp(self.base / "from-env")
        os.environ["MP_DIR"] = str(mp)
        self.assertEqual(find_micropython(None), mp.resolve())

    def test_workspace_nested(self) -> None:
        ws = self.base / "ws"
        mp = _touch_mp(ws / "micropython")
        found = find_micropython(None, workspace=str(ws))
        self.assertEqual(found, mp.resolve())

    def test_workspace_is_tree(self) -> None:
        mp = _touch_mp(self.base / "checkout")
        found = find_micropython(None, workspace=str(mp))
        self.assertEqual(found, mp.resolve())

    def test_home_micropython(self) -> None:
        mp = _touch_mp(self.base / "home" / "micropython")
        self.assertEqual(find_micropython(None), mp.resolve())

    def test_no_gh_layout_required(self) -> None:
        # Empty env / no candidates → None (never invents ~/gh/… paths).
        self.assertIsNone(find_micropython(None, workspace=None))


class TestFindSdkTree(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for k in ("IDF_PATH", "IDF_DIR", "EMSDK", "EMSDK_DIR"):
            os.environ.pop(k, None)
        self.state = mock.patch("mpftp.firmware.load_state", return_value={})
        self.state.start()

    def tearDown(self) -> None:
        self.state.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_idf_from_workspace_symlink_style(self) -> None:
        ws = self.base / "ws"
        idf = ws / "esp-idf"
        idf.mkdir(parents=True)
        (idf / "export.sh").write_text("#!/bin/true\n", encoding="utf-8")
        found = find_idf(None, ws)
        self.assertEqual(found, idf.resolve())

    def test_idf_from_env_not_home(self) -> None:
        elsewhere = self.base / "opt" / "esp-idf"
        elsewhere.mkdir(parents=True)
        (elsewhere / "export.sh").write_text("#!/bin/true\n", encoding="utf-8")
        # A decoy home path must not win over env.
        decoy = self.base / "home-decoy" / "esp" / "esp-idf"
        decoy.mkdir(parents=True)
        (decoy / "export.sh").write_text("#!/bin/true\n", encoding="utf-8")
        os.environ["IDF_PATH"] = str(elsewhere)
        with mock.patch("mpftp.firmware.HOME", self.base / "home-decoy"):
            found = find_idf(None, workspace=None)
        self.assertEqual(found, elsewhere.resolve())

    def test_emsdk_workspace(self) -> None:
        ws = self.base / "ws"
        em = ws / "emsdk"
        em.mkdir(parents=True)
        (em / "emsdk_env.sh").write_text("#\n", encoding="utf-8")
        self.assertEqual(find_emsdk(None, ws), em.resolve())

    def test_sdk_missing_without_home_hunt(self) -> None:
        # No env, no workspace child → None even if HOME has esp-idf.
        home = self.base / "fake-home"
        idf = home / "esp" / "esp-idf"
        idf.mkdir(parents=True)
        (idf / "export.sh").write_text("#!/bin/true\n", encoding="utf-8")
        with mock.patch("mpftp.firmware.HOME", home):
            self.assertIsNone(find_idf(None, workspace=None))
            self.assertIsNone(find_emsdk(None, workspace=None))

    def test_generic_find_sdk_tree(self) -> None:
        ws = self.base / "ws"
        tree = ws / "my-sdk"
        tree.mkdir(parents=True)
        (tree / "marker.txt").write_text("x", encoding="utf-8")
        found = find_sdk_tree(
            hint=None,
            env_keys=("MY_SDK",),
            workspace=ws,
            dirname="my-sdk",
            marker_file="marker.txt",
        )
        self.assertEqual(found, tree.resolve())


class TestDiscoverCmods(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_cmake_only(self) -> None:
        mod = self.ws / "graphics"
        mod.mkdir()
        (mod / "micropython.cmake").write_text("#\n", encoding="utf-8")
        out = discover_cmods(self.ws)
        self.assertEqual([m["name"] for m in out["modules"]], ["graphics"])
        self.assertEqual(out["modules"][0]["kind"], "cmake")
        self.assertFalse(out["modules"][0]["hasManifest"])

    def test_manifest_only(self) -> None:
        mod = self.ws / "pdwidgets"
        mod.mkdir()
        (mod / "manifest.py").write_text("freeze('.')\n", encoding="utf-8")
        out = discover_cmods(self.ws)
        self.assertEqual([m["name"] for m in out["modules"]], ["pdwidgets"])
        self.assertEqual(out["modules"][0]["kind"], "manifest")
        self.assertTrue(out["modules"][0]["hasManifest"])
        self.assertFalse(out["modules"][0]["hasCmake"])
        self.assertFalse(out["modules"][0]["hasMk"])

    def test_mk_and_manifest(self) -> None:
        mod = self.ws / "usdl2"
        mod.mkdir()
        (mod / "micropython.mk").write_text("#\n", encoding="utf-8")
        (mod / "manifest.py").write_text("freeze('.')\n", encoding="utf-8")
        out = discover_cmods(self.ws)
        m = out["modules"][0]
        self.assertEqual(m["kind"], "make")
        self.assertTrue(m["hasMk"])
        self.assertTrue(m["hasManifest"])

    def test_skips_root_aggregators_as_modules(self) -> None:
        (self.ws / "micropython.cmake").write_text("# agg\n", encoding="utf-8")
        (self.ws / "manifest-micropython.py").write_text("# agg\n", encoding="utf-8")
        out = discover_cmods(self.ws)
        self.assertEqual(out["modules"], [])
        self.assertTrue(out["hasAggregator"])
        self.assertTrue(out["hasManifest"])
        self.assertTrue(out["manifest"].endswith("manifest-micropython.py"))


if __name__ == "__main__":
    unittest.main()
