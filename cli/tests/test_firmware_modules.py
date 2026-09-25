"""Module selection for firmware builds (mpftp#36): discovery, presets, overlays,
the generated manifest, and the make arguments a selection turns into."""

from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from mpftp import firmware
from mpftp.firmware import (
    build_tree,
    discover_modules,
    render_selection_manifest,
    resolve_modules,
    resolve_preset,
    resolve_target,
    target_make_args,
    upstream_prologue,
)


def _w(p: Path, text: str = "") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def make_workspace(base: Path) -> Path:
    """A workspace like the real one: MicroPython, an overlay, and modules."""
    mp = base / "ws" / "micropython"
    _w(mp / "py" / "mpconfig.h")
    # esp32: a board port with one upstream board.
    esp = mp / "ports" / "esp32"
    _w(esp / "Makefile")
    _w(esp / "boards" / "manifest.py", "# port-wide\n")
    _w(esp / "boards" / "ESP32_GENERIC" / "mpconfigboard.cmake")
    # unix: a variant port.
    unix = mp / "ports" / "unix"
    _w(unix / "Makefile")
    _w(unix / "variants" / "manifest.py", "# port-wide variants\n")
    _w(unix / "variants" / "standard" / "mpconfigvariant.mk")
    _w(unix / "variants" / "standard" / "manifest.py", "# standard\n")

    ws = mp.parent
    # C module named by its own manifest, depending on another.
    _w(ws / "audiodsp" / "manifest.py", 'c_module(".")\n')
    _w(ws / "audiodsp" / "micropython.mk")
    _w(ws / "audioif" / "manifest.py", 'include("../audiodsp/manifest.py")\nc_module(".")\n')
    # Freeze-only.
    _w(ws / "palettes" / "manifest.py", 'package("palettes", base_path="./lib")\n')
    # CircuitPython-only: skipped.
    _w(ws / "lvgl-circuitpython" / "manifest.py", 'module("x.py")\n')
    _w(ws / "lvgl-circuitpython" / "apply_cp_patches.sh")
    # Excluded by name (mip-installed, never frozen).
    _w(ws / "pydevices" / "manifest.py", 'package("lib")\n')
    # A manifest that does nothing: not a module.
    _w(ws / "notes" / "manifest.py", "# nothing\n")

    # The overlay: presets, a board and a variant.
    ov = ws / "micropython-pydevices"
    _w(ov / "manifests" / "headless.py", "# prologue\n")
    _w(ov / "manifests" / "audio.py", 'include("../../audioif/manifest.py")\n')
    _w(ov / "manifests" / "kitchen-sink.py", "import os\nfor _ in os.listdir('.'):\n    pass\n")
    board = ov / "boards" / "esp32" / "WAVESHARE_ESP32_P4_PANEL"
    _w(board / "mpconfigboard.cmake")
    _w(board / "mpconfigvariant_PRE_REV3_C6_WIFI.cmake")
    _w(board / "manifest.py", 'include("../../../manifests/kitchen-sink.py")\n')
    _w(ov / "variants" / "unix" / "pydevices" / "mpconfigvariant.mk")
    return mp


class ModulesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.mp = make_workspace(self.base)
        self.ws = self.mp.parent
        # A second root, like ~/gh/bdbarnett, holding a legacy usermod whose
        # manifest does not name its C half.
        self.personal = self.base / "personal"
        _w(self.personal / "earful" / "manifest.py", "# deliberately empty\n")
        _w(self.personal / "earful" / "micropython.cmake")
        # Isolate from the developer's ~/.mpftp.
        self.cfg = mock.patch.object(firmware.config, "load", return_value={})
        self.cfg.start()
        self.gen = mock.patch.object(firmware, "GENERATED_MANIFEST_DIR", self.base / "state")
        self.gen.start()

    def tearDown(self) -> None:
        self.gen.stop()
        self.cfg.stop()
        self.tmp.cleanup()

    def discover(self, extra=None) -> dict:
        return discover_modules(self.mp, extra)


class TestDiscovery(ModulesTestCase):
    def test_finds_c_and_freeze_modules_and_skips_the_rest(self) -> None:
        d = self.discover()
        by = {m["name"]: m for m in d["modules"]}
        self.assertEqual(sorted(by), ["audiodsp", "audioif", "palettes"])
        self.assertTrue(by["audiodsp"]["hasC"])
        self.assertTrue(by["audiodsp"]["cNamedByManifest"])
        self.assertTrue(by["palettes"]["freezeOnly"])
        self.assertFalse(by["palettes"]["hasC"])

    def test_dependencies_are_read_from_include_lines(self) -> None:
        by = {m["name"]: m for m in self.discover()["modules"]}
        self.assertEqual(by["audioif"]["requires"], ["audiodsp"])

    def test_overlay_is_not_a_module_and_its_manifests_are_presets(self) -> None:
        d = self.discover()
        self.assertEqual([o["name"] for o in d["overlays"]], ["micropython-pydevices"])
        presets = {p["name"]: p for p in d["presets"]}
        self.assertEqual(sorted(presets), ["audio", "headless", "kitchen-sink"])
        self.assertEqual(presets["audio"]["requires"], ["audioif"])
        self.assertTrue(presets["kitchen-sink"]["scansWorkspace"])
        self.assertFalse(presets["headless"]["scansWorkspace"])

    def test_extra_roots_and_configured_roots(self) -> None:
        self.assertNotIn("earful", [m["name"] for m in self.discover()["modules"]])
        d = self.discover(str(self.personal))
        earful = next(m for m in d["modules"] if m["name"] == "earful")
        self.assertTrue(earful["hasC"])
        self.assertFalse(earful["cNamedByManifest"])
        with mock.patch.object(
            firmware.config, "load", return_value={"firmwareModuleRoots": [str(self.personal)]}
        ):
            self.assertIn("earful", [m["name"] for m in self.discover()["modules"]])

    def test_same_name_in_two_roots_is_qualified(self) -> None:
        _w(self.personal / "palettes" / "manifest.py", 'module("p.py")\n')
        names = [m["name"] for m in self.discover(str(self.personal))["modules"]]
        self.assertIn("palettes", names)
        self.assertIn("personal/palettes", names)

    def test_modules_resolve_by_name_or_path(self) -> None:
        d = self.discover()
        found, unknown = resolve_modules(d, ["audioif", str(self.personal / "earful"), "nope"])
        self.assertEqual([m["name"] for m in found], ["audioif", "earful"])
        self.assertEqual(unknown, ["nope"])

    def test_preset_resolves_by_name_or_path(self) -> None:
        d = self.discover()
        self.assertTrue(str(resolve_preset(d, "audio")).endswith("manifests/audio.py"))
        other = _w(self.base / "mine.py", "# mine\n")
        self.assertEqual(resolve_preset(d, str(other)), other)
        self.assertIsNone(resolve_preset(d, "missing"))


class TestTargets(ModulesTestCase):
    def test_tree_lists_overlay_boards_and_variants_beside_upstream(self) -> None:
        tree = {p["port"]: p for p in build_tree(self.mp, [self.ws / "micropython-pydevices"])}
        boards = {b["board"]: b for b in tree["esp32"]["boards"]}
        self.assertNotIn("source", boards["ESP32_GENERIC"])
        p4 = boards["WAVESHARE_ESP32_P4_PANEL"]
        self.assertEqual(p4["source"], "micropython-pydevices")
        self.assertEqual(p4["variants"], ["PRE_REV3_C6_WIFI"])
        self.assertEqual(tree["unix"]["variants"], ["standard", "pydevices"])
        self.assertIn("pydevices", tree["unix"]["variantSources"])

    def test_overlay_board_builds_with_board_dir(self) -> None:
        ov = [self.ws / "micropython-pydevices"]
        t = resolve_target(self.mp, "esp32", "WAVESHARE_ESP32_P4_PANEL", "PRE_REV3_C6_WIFI",
                           overlays=ov)
        self.assertEqual(
            target_make_args("boards", t),
            [
                f"BOARD_DIR={ov[0] / 'boards/esp32/WAVESHARE_ESP32_P4_PANEL'}",
                "BOARD=WAVESHARE_ESP32_P4_PANEL",
                "BOARD_VARIANT=PRE_REV3_C6_WIFI",
            ],
        )
        up = resolve_target(self.mp, "esp32", "ESP32_GENERIC", "", overlays=ov)
        self.assertEqual(target_make_args("boards", up), ["BOARD=ESP32_GENERIC"])

    def test_explicit_board_dir_names_the_board(self) -> None:
        d = self.base / "elsewhere" / "MY_BOARD"
        d.mkdir(parents=True)
        t = resolve_target(self.mp, "esp32", "", "", board_dir=str(d))
        self.assertEqual(t["board"], "MY_BOARD")
        self.assertIn(f"BOARD_DIR={d}", target_make_args("boards", t))

    def test_overlay_variant_builds_with_variant_dir(self) -> None:
        ov = [self.ws / "micropython-pydevices"]
        t = resolve_target(self.mp, "unix", "", "pydevices", overlays=ov)
        self.assertEqual(
            target_make_args("variants", t),
            [f"VARIANT_DIR={ov[0] / 'variants/unix/pydevices'}", "VARIANT=pydevices"],
        )

    def test_prologue_is_upstream_content_never_an_overlay_default(self) -> None:
        esp = self.mp / "ports" / "esp32"
        unix = self.mp / "ports" / "unix"
        self.assertEqual(
            upstream_prologue(esp, "boards", "WAVESHARE_ESP32_P4_PANEL", "", False, True),
            esp / "boards" / "manifest.py",
        )
        # The default variant and an overlay variant both get upstream's standard.
        std = unix / "variants" / "standard" / "manifest.py"
        self.assertEqual(upstream_prologue(unix, "variants", "", "", True, True), std)
        self.assertEqual(upstream_prologue(unix, "variants", "", "pydevices", True, False), std)


class TestGeneratedManifest(ModulesTestCase):
    def test_preset_plus_modules(self) -> None:
        d = self.discover(str(self.personal))
        mods, _ = resolve_modules(d, ["earful"])
        text = render_selection_manifest(
            None, resolve_preset(d, "kitchen-sink"), mods, "preset kitchen-sink + earful"
        )
        lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
        self.assertEqual(
            lines,
            [
                f'include("{self.ws}/micropython-pydevices/manifests/kitchen-sink.py")',
                f'include("{self.personal}/earful/manifest.py")',
                # earful's manifest does not name its C half, so the selection does.
                f'c_module("{self.personal}/earful")',
            ],
        )

    def test_modules_without_a_preset_open_with_upstream_content(self) -> None:
        d = self.discover()
        mods, _ = resolve_modules(d, ["audioif"])
        prologue = self.mp / "ports" / "esp32" / "boards" / "manifest.py"
        text = render_selection_manifest(prologue, None, mods, "x")
        lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
        self.assertEqual(
            lines,
            [f'include("{prologue}")', f'include("{self.ws}/audioif/manifest.py")'],
        )


class TestBuildArgs(ModulesTestCase):
    """What do_build hands to make, with the shell and toolchains stubbed out."""

    def build(self, **kw) -> tuple[list[str], list[dict]]:
        lines, records, _envs = self.build_env(**kw)
        return lines, records

    def build_env(self, **kw) -> tuple[list[str], list[dict], list[dict]]:
        ns = argparse.Namespace(
            mp=str(self.mp), port="esp32", board="", variant="", board_dir="",
            variant_dir="", build_dir="", module_roots=None, preset="", modules="",
            jobs=0, clean=False, idf=None, emsdk=None, toolchain_bins="", autosize=False,
        )
        for k, v in kw.items():
            setattr(ns, k, v)
        scripts: list[list[str]] = []
        envs: list[dict] = []

        def fake_shell(lines, cwd, env):
            scripts.append(list(lines))
            envs.append(dict(env))
            return 0, ""

        out = io.StringIO()
        with mock.patch.object(firmware, "resolve_build_toolchains", return_value=(None, [])), \
                mock.patch.object(firmware, "find_idf", return_value=self.base), \
                mock.patch.object(firmware, "idf_version_mismatch", return_value=None), \
                mock.patch.object(firmware, "_run_shell", lambda *a: fake_shell(*a)[0]), \
                mock.patch.object(firmware, "_run_shell_cap", fake_shell), \
                mock.patch.object(firmware, "save_state"), \
                mock.patch.object(firmware, "log_activity"), \
                redirect_stdout(out):
            firmware.do_build(ns)
        records = [json.loads(ln) for ln in out.getvalue().splitlines() if ln.strip()]
        return [ln for s in scripts for ln in s], records, envs

    def test_p4_kitchen_sink_plus_earful(self) -> None:
        lines, records = self.build(
            board="WAVESHARE_ESP32_P4_PANEL", variant="PRE_REV3_C6_WIFI",
            preset="kitchen-sink", modules="earful", module_roots=str(self.personal),
        )
        make_all = next(ln for ln in lines if " all " in ln)
        self.assertIn("BOARD_DIR=", make_all)
        self.assertIn("BOARD=WAVESHARE_ESP32_P4_PANEL", make_all)
        self.assertIn("BOARD_VARIANT=PRE_REV3_C6_WIFI", make_all)
        manifest = self.base / "state" / "manifest-esp32-WAVESHARE_ESP32_P4_PANEL-PRE_REV3_C6_WIFI.py"
        self.assertIn(f"FROZEN_MANIFEST={manifest}", make_all)
        self.assertIn("kitchen-sink.py", manifest.read_text())
        self.assertIn("earful", manifest.read_text())
        self.assertTrue(records[-1]["type"] == "result")

    def test_no_selection_builds_the_targets_own_manifest(self) -> None:
        lines, _ = self.build(board="ESP32_GENERIC")
        make_all = next(ln for ln in lines if " all " in ln)
        self.assertNotIn("FROZEN_MANIFEST=", make_all)
        self.assertNotIn("USER_C_MODULES=", make_all)

    def test_build_dir_override(self) -> None:
        bdir = self.base / "scratch-build"
        lines, _ = self.build(board="ESP32_GENERIC", build_dir=str(bdir))
        make_all = next(ln for ln in lines if " all " in ln)
        self.assertIn(f"BUILD={bdir}", make_all)

    def test_build_dir_keeps_mpy_cross_out_of_it(self) -> None:
        # mpftp#46: BUILD= reaches the port's own mpy-cross sub-make through
        # MAKEFLAGS. With MICROPY_MPYCROSS set, the port never starts one.
        bdir = self.base / "scratch-build"
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MICROPY_MPYCROSS", None)
            os.environ["BUILD"] = "leaked-from-the-shell"
            lines, _, envs = self.build_env(board="ESP32_GENERIC", build_dir=str(bdir))
        mpy_cross = str(self.mp / "mpy-cross" / "build" / "mpy-cross")
        self.assertTrue(envs)
        for env in envs:
            self.assertEqual(env.get("MICROPY_MPYCROSS"), mpy_cross)
        prebuild = next(ln for ln in lines if "/mpy-cross\"" in ln)
        self.assertIn("BUILD=build ", prebuild)

    def test_default_build_dir_leaves_mpy_cross_to_the_port(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MICROPY_MPYCROSS", None)
            _, _, envs = self.build_env(board="ESP32_GENERIC")
        self.assertTrue(envs)
        for env in envs:
            self.assertNotIn("MICROPY_MPYCROSS", env)

    def test_unknown_names_fail_before_make(self) -> None:
        for kw, word in (({"modules": "nope"}, "Unknown module"),
                         ({"preset": "nope"}, "Unknown preset"),
                         ({"board": "NOPE"}, "Unknown board")):
            lines, records = self.build(**{"board": "ESP32_GENERIC", **kw})
            self.assertEqual(lines, [])
            self.assertFalse(records[-1]["ok"])
            self.assertIn(word, records[-1]["error"])


class TestCliText(unittest.TestCase):
    def test_modules_text_names_kinds_and_dependencies(self) -> None:
        from mpftp.cli import format_modules

        text = format_modules(
            {
                "roots": ["/ws"],
                "modules": [
                    {"name": "audioif", "hasC": True, "path": "/ws/audioif",
                     "requires": ["audiodsp"]},
                    {"name": "palettes", "hasC": False, "path": "/ws/palettes", "requires": []},
                ],
                "presets": [{"name": "audio", "requires": ["audiodsp", "audioif"]}],
            }
        )
        self.assertIn("audioif", text)
        self.assertIn("needs audiodsp", text)
        self.assertIn("freeze-only", text)
        self.assertIn("audio  audiodsp, audioif", text)

    def test_tree_text_walks_ports_then_boards_then_stops(self) -> None:
        from mpftp.cli import format_tree

        tree = {
            "micropython": "/mp",
            "ports": [
                {"port": "esp32", "kind": "boards", "flashable": True, "flasher": "esptool",
                 "boards": [{"board": "P4", "variants": ["V"], "source": "overlay"}],
                 "variants": []},
            ],
        }
        self.assertIn("esp32", format_tree(tree, "", "", ""))
        self.assertIn("[overlay]", format_tree(tree, "esp32", "", ""))
        self.assertIsNone(format_tree(tree, "esp32", "P4", ""))


if __name__ == "__main__":
    unittest.main()
