"""`mpftp firmware build` runs micropython-pydevices' build_mp.py (mpftp#75).

Every test here runs against a fake build_mp.py: a script with the same
listing functions as the real one (ports(), boards(), variants(),
module_choices(), the cp_ twins) and a main() that records the command line it
was given, then succeeds, refuses or crashes as the test asks. No toolchain,
no board and no real checkout are needed.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from mpftp import cli, firmware
from mpftp import firmware_build as fb

FAKE_BUILD_MP = textwrap.dedent(
    '''
    import json, os, sys
    from pathlib import Path

    REPO = Path(__file__).resolve().parent
    OUT_DIR = Path(os.environ.get("OUT_DIR", REPO / "builds")).resolve()
    MODULES_DIR = REPO / "modules"
    MP = REPO / "micropython"
    CP = REPO / "deps" / "circuitpython"
    CP_BOARDS = REPO / "boards" / "circuitpython"
    FLASH_SIZES = ("4MB", "8MB", "16MB")
    DEFAULT_VARIANT = {"unix": "standard"}

    print("the listing must not see this")

    def die(msg):
        sys.exit(f"build_mp.py: {msg}")

    def workspace():
        return None

    def ensure_micropython(ws):
        pass

    def read_lock(name):
        return [("audiodsp", "url", "sha"), ("ulab", "url", "sha")]

    def ports():
        return ["esp32", "minimal", "unix"]

    def is_board_port(port):
        return port == "esp32"

    def boards(port):
        return ["ESP32_GENERIC_P4", "ESP32_GENERIC_S3"]

    def variants(port, board):
        if board == "ESP32_GENERIC_P4":
            return ["C6_WIFI", "PRE_REV3_C6_WIFI"]
        if board:
            return ["SPIRAM_OCT"]
        if port == "minimal":
            raise FileNotFoundError("no variants dir")
        return ["pydevices", "standard"]

    def module_choices():
        return sorted(p.name for p in MODULES_DIR.iterdir() if p.is_dir())

    def cp_ports():
        return ["espressif", "unix"]

    def cp_boards(port):
        return ["adafruit_qtpy_esp32_pico"] if port == "espressif" else []

    def cp_variants(port):
        return ["coverage"] if port == "unix" else []

    def main():
        (REPO / "calls.json").write_text(json.dumps({
            "argv": sys.argv[1:],
            "tty": sys.stdin.isatty(),
            "OUT_DIR": os.environ.get("OUT_DIR"),
            "JOBS": os.environ.get("JOBS"),
        }))
        mode = (REPO / "mode").read_text().strip() if (REPO / "mode").exists() else "ok"
        args = sys.argv[1:]
        def opt(name):
            return args[args.index(name) + 1] if name in args else ""
        if mode == "die":
            die("no board 'NOPE' for esp32 (have: ESP32_GENERIC_P4, ESP32_GENERIC_S3)")
        if mode == "prompt":
            die("Board:: not given, and there is no terminal to ask on")  # its prompt is "Board:"
        if mode == "make":
            print("compiling audiodsp ...")
            print("../modules/audiodsp/src/biquad.c:12:3: error: unknown type name 'fooo'")
            print("make: *** [Makefile:12: all] Error 2")
            die("the build failed (make exit 2)")
        if mode == "crash":
            raise KeyError("CONFIG_PARTITION_TABLE_FILENAME")
        port, board, variant = opt("--port"), opt("--board"), opt("--variant")
        build = OUT_DIR / port / board / (variant or DEFAULT_VARIANT.get(port, "default"))
        build.mkdir(parents=True, exist_ok=True)
        name = "firmware.bin" if port == "esp32" else "micropython"
        (build / name).write_bytes(b"x" * 10)
        print("")
        print(f"Built into {build}")
        print(f"  {build / name}")

    if __name__ == "__main__":
        main()
    '''
)


def make_checkout(root: Path) -> Path:
    bs = root / "micropython-pydevices"
    board = bs / "micropython" / "ports" / "esp32" / "boards" / "ESP32_GENERIC_P4"
    board.mkdir(parents=True)
    (board / "board.json").write_text('{"mcu": "esp32p4"}')
    for name in ("all", "audiodsp", "tflite", "castif"):
        (bs / "modules" / name).mkdir(parents=True)
    (bs / "modules" / "tflite" / "OPT_IN").write_text("")
    (bs / "modules" / "audiodsp" / "micropython.mk").write_text("")
    (bs / "modules" / "castif" / "micropython.cmake").write_text("")
    (bs / "build_mp.py").write_text(FAKE_BUILD_MP)
    return bs


def run_engine(argv: list[str]) -> list[dict]:
    """The engine's NDJSON (or single JSON) output, parsed."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        firmware.main(argv)
    text = buf.getvalue().strip()
    try:
        return [json.loads(text)]
    except ValueError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]


@contextlib.contextmanager
def on_a_terminal():
    """Our stdin is a terminal, as it is when someone types the command: the
    build must still not hand it to build_mp.py, which would sit and prompt."""
    if os.name != "posix":
        yield
        return
    import pty

    leader, follower = pty.openpty()
    saved = os.dup(0)
    os.dup2(follower, 0)
    try:
        yield
    finally:
        os.dup2(saved, 0)
        for fd in (saved, leader, follower):
            os.close(fd)


def result_of(msgs: list[dict]) -> dict:
    return [m for m in msgs if m.get("type") == "result"][-1]


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bs = make_checkout(self.root)
        self.cwd = self.root / "elsewhere"
        self.cwd.mkdir()
        for target, value in (
            ("_configured_build_system", ""),
            ("save_state", None),
            ("log_activity", None),
        ):
            p = mock.patch.object(firmware, target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        env = {k: v for k, v in os.environ.items() if k not in ("OUT_DIR", "JOBS")}
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        old = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, old)
        self.addCleanup(self.tmp.cleanup)

    def calls(self) -> dict:
        return json.loads((self.bs / "calls.json").read_text())

    def mode(self, m: str) -> None:
        (self.bs / "mode").write_text(m)


class ArgumentMapping(Base):
    def test_every_choice_reaches_build_mp(self) -> None:
        argv = fb.build_argv(
            self.bs, port="esp32", board="ESP32_GENERIC_P4", variant="C6_WIFI",
            modules="audiodsp, displayif", flash="16MB", clean=True, autosize=False,
            make_args=["V=1"], python="py",
        )
        self.assertEqual(argv, [
            "py", str(self.bs / "build_mp.py"),
            "--port", "esp32", "--board", "ESP32_GENERIC_P4", "--variant", "C6_WIFI",
            "--modules=audiodsp,displayif", "--flash", "16MB",
            "--no-autosize", "--clean", "V=1",
        ])

    def test_circuitpython_is_one_flag(self) -> None:
        argv = fb.build_argv(self.bs, port="espressif", board="adafruit_qtpy_esp32_pico",
                             modules="audiodsp", interpreter="circuitpython", python="py")
        self.assertEqual(argv[2:6], ["--interpreter", "circuitpython", "--port", "espressif"])
        self.assertNotIn("--interpreter", fb.build_argv(self.bs, port="unix", python="py"))

    def test_no_modules_is_said_out_loud(self) -> None:
        # build_mp.py would otherwise ask, and mpftp gives it no terminal.
        self.assertIn("--modules=", fb.build_argv(self.bs, port="unix", python="py"))

    def test_module_paths_are_made_absolute_and_names_kept(self) -> None:
        spec = fb.module_spec("audiodsp,./earful,~/x,audiodsp,,")
        self.assertEqual(
            spec.split(","),
            ["audiodsp", str((self.cwd / "earful").resolve()), str(Path("~/x").expanduser().resolve())],
        )
        self.assertEqual(fb.module_spec(["all"]), "all")

    def test_no_port_and_a_wrong_interpreter_are_refused(self) -> None:
        with self.assertRaises(fb.BuildSystemError):
            fb.build_argv(self.bs, port="")
        with self.assertRaises(fb.BuildSystemError):
            fb.build_argv(self.bs, port="unix", interpreter="pyodide")

    def test_cli_flags_become_engine_flags(self) -> None:
        ns = cli.build_parser().parse_args([
            "firmware", "build", "--build-system", str(self.bs), "--interpreter", "circuitpython",
            "--port", "espressif", "--board", "b", "--modules", "audiodsp", "--flash", "8MB",
            "--no-autosize", "--clean", "--jobs", "3", "--make-arg", "CIRCUITPY_ULAB=0",
            "--out-dir", "/o",
        ])
        extra = cli.build_args(ns)
        engine = firmware.build_parser().parse_args(["build", *extra])
        self.assertEqual(
            (engine.build_system, engine.interpreter, engine.port, engine.board, engine.modules,
             engine.flash, engine.autosize, engine.clean, engine.jobs, engine.make_arg, engine.out_dir),
            (str(self.bs), "circuitpython", "espressif", "b", "audiodsp",
             "8MB", False, True, 3, ["CIRCUITPY_ULAB=0"], "/o"),
        )


class Building(Base):
    def build(self, *extra: str) -> dict:
        return result_of(run_engine(["build", "--build-system", str(self.bs), *extra]))

    def test_a_build_runs_build_mp_with_the_selection(self) -> None:
        with on_a_terminal():
            res = self.build("--port", "esp32", "--board", "ESP32_GENERIC_P4", "--variant", "C6_WIFI",
                             "--modules", "audiodsp,./earful", "--flash", "16MB", "--jobs", "2")
        seen = self.calls()
        self.assertEqual(seen["argv"], [
            "--port", "esp32", "--board", "ESP32_GENERIC_P4", "--variant", "C6_WIFI",
            f"--modules=audiodsp,{(self.cwd / 'earful').resolve()}", "--flash", "16MB",
        ])
        self.assertFalse(seen["tty"], "build_mp.py must never get a terminal to prompt on")
        self.assertEqual(seen["JOBS"], "2")
        self.assertTrue(res["ok"], res)
        built = self.bs / "builds" / "esp32" / "ESP32_GENERIC_P4" / "C6_WIFI"
        self.assertEqual(res["buildDir"], str(built))
        self.assertEqual(res["artifact"], str(built / "firmware.bin"))
        self.assertEqual(res["flashOffset"], "0x2000")  # from the board's board.json

    def test_out_dir_is_build_mps_out_dir_and_artifact_finds_it(self) -> None:
        out = self.root / "mine"
        res = self.build("--port", "unix", "--out-dir", str(out))
        self.assertEqual(self.calls()["OUT_DIR"], str(out.resolve()))
        self.assertEqual(res["artifact"], str(out / "unix" / "standard" / "micropython"))
        art = run_engine(["artifact", "--build-system", str(self.bs), "--port", "unix",
                          "--out-dir", str(out)])[0]
        self.assertTrue(art["ready"])
        self.assertEqual(art["artifact"], res["artifact"])

    def test_build_mps_refusal_is_the_error(self) -> None:
        self.mode("die")
        res = self.build("--port", "esp32", "--board", "NOPE")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "no board 'NOPE' for esp32 (have: ESP32_GENERIC_P4, ESP32_GENERIC_S3)")

    def test_a_missing_choice_names_the_flag_and_the_list(self) -> None:
        self.mode("prompt")
        res = self.build("--port", "esp32")
        self.assertEqual(res["error"], "build_mp.py needs --board (see `mpftp firmware list --port esp32`)")

    def test_a_make_failure_says_which_line_failed(self) -> None:
        self.mode("make")
        res = self.build("--port", "unix", "--modules", "audiodsp")
        self.assertEqual(res["error"], "the build failed (make exit 2)")
        self.assertEqual(res["detail"], "../modules/audiodsp/src/biquad.c:12:3: error: unknown type name 'fooo'")
        self.assertEqual(res["returncode"], 1)

    def test_a_crash_reports_its_exception(self) -> None:
        self.mode("crash")
        res = self.build("--port", "unix")
        self.assertIn("KeyError: 'CONFIG_PARTITION_TABLE_FILENAME'", res["error"])

    def test_presets_are_refused_with_what_replaces_them(self) -> None:
        res = self.build("--port", "unix", "--preset", "kitchen-sink")
        self.assertFalse(res["ok"])
        self.assertIn("all", res["error"])
        self.assertFalse((self.bs / "calls.json").exists())

    def test_clean_removes_only_that_targets_dir(self) -> None:
        self.build("--port", "unix")
        self.build("--port", "esp32", "--board", "ESP32_GENERIC_P4")
        res = result_of(run_engine(["clean", "--build-system", str(self.bs), "--port", "unix"]))
        self.assertTrue(res["ok"])
        self.assertFalse((self.bs / "builds" / "unix" / "standard").exists())
        self.assertTrue((self.bs / "builds" / "esp32" / "ESP32_GENERIC_P4" / "default").exists())


class Listing(Base):
    def test_micropython_ports_boards_and_variants(self) -> None:
        tree = run_engine(["tree", "--build-system", str(self.bs)])[0]
        ports = {p["port"]: p for p in tree["ports"]}
        self.assertEqual(list(ports), ["esp32", "minimal", "unix"])
        self.assertEqual(ports["esp32"]["kind"], "boards")
        self.assertEqual(
            {b["board"]: b["variants"] for b in ports["esp32"]["boards"]},
            {"ESP32_GENERIC_P4": ["C6_WIFI", "PRE_REV3_C6_WIFI"], "ESP32_GENERIC_S3": ["SPIRAM_OCT"]},
        )
        self.assertEqual(ports["unix"]["variants"], ["pydevices", "standard"])
        self.assertEqual((ports["minimal"]["kind"], ports["minimal"]["variants"]), ("plain", []))
        self.assertTrue(ports["esp32"]["flashable"])
        self.assertFalse(ports["unix"]["flashable"])
        self.assertEqual(tree["flashSizes"], ["4MB", "8MB", "16MB"])

    def test_modules_are_build_mps_with_the_lock_ones_not_yet_fetched(self) -> None:
        info = run_engine(["modules", "--build-system", str(self.bs)])[0]
        mods = {m["name"]: m for m in info["modules"]}
        self.assertEqual(list(mods)[0], "all")
        self.assertEqual(sorted(mods), ["all", "audiodsp", "castif", "tflite", "ulab"])
        self.assertTrue(mods["tflite"]["optIn"])
        self.assertFalse(mods["ulab"]["present"])
        self.assertTrue(mods["audiodsp"]["hasC"] and mods["castif"]["hasC"])
        self.assertTrue(mods["tflite"]["freezeOnly"])
        self.assertEqual(info["presets"], [])
        text = cli.format_modules(info)
        self.assertIn("every module but the opt-in ones", text)
        self.assertIn("(opt-in)", text)
        self.assertIn("(fetched on first build)", text)

    def test_circuitpython_lists_its_own_ports_and_our_boards(self) -> None:
        cp = self.bs / "deps" / "circuitpython" / "ports"
        cp.mkdir(parents=True)
        ours = self.bs / "boards" / "circuitpython" / "espressif" / "waveshare_esp32s3_touch_lcd_7"
        ours.mkdir(parents=True)
        (ours / "mpconfigboard.mk").write_text("")
        tree = run_engine(["tree", "--build-system", str(self.bs), "--interpreter", "circuitpython"])[0]
        ports = {p["port"]: p for p in tree["ports"]}
        self.assertEqual(
            [b["board"] for b in ports["espressif"]["boards"]],
            ["adafruit_qtpy_esp32_pico", "waveshare_esp32s3_touch_lcd_7"],
        )
        self.assertEqual(ports["unix"]["variants"], ["coverage"])
        self.assertFalse(ports["espressif"]["flashable"])

    def test_circuitpython_not_fetched_yet_says_so(self) -> None:
        tree = run_engine(["tree", "--build-system", str(self.bs), "--interpreter", "circuitpython"])[0]
        self.assertEqual(tree["ports"], [])
        self.assertIn("deps/circuitpython", tree["error"])

    def test_tree_text_walks_ports_then_boards(self) -> None:
        tree = run_engine(["tree", "--build-system", str(self.bs)])[0]
        top = cli.format_tree(tree, "", "", "")
        self.assertIn("esp32", top)
        self.assertIn("Next: mpftp firmware list --port PORT", top)
        boards = cli.format_tree(tree, "esp32", "", "")
        self.assertIn("ESP32_GENERIC_P4  variants: C6_WIFI, PRE_REV3_C6_WIFI", boards)
        self.assertIsNone(cli.format_tree(tree, "esp32", "ESP32_GENERIC_P4", ""))

    def test_an_old_build_mp_says_to_update(self) -> None:
        text = (self.bs / "build_mp.py").read_text().replace("def module_choices", "def gone")
        (self.bs / "build_mp.py").write_text(text)
        with self.assertRaises(fb.BuildSystemError) as cm:
            fb.offer(self.bs)
        self.assertIn("module_choices", str(cm.exception))
        self.assertIn("update micropython-pydevices", str(cm.exception))


class FindingTheCheckout(Base):
    def test_beside_a_workspace_root_or_the_micropython_tree(self) -> None:
        self.assertEqual(fb.find_build_system(workspace=[str(self.root)]), self.bs.resolve())
        self.assertEqual(fb.find_build_system(workspace=str(self.bs / "modules") + os.pathsep + str(self.bs)),
                         self.bs.resolve())
        self.assertEqual(fb.find_build_system(mp=str(self.bs / "micropython")), self.bs.resolve())
        sibling = self.root / "micropython"
        sibling.mkdir()
        self.assertEqual(fb.find_build_system(mp=str(sibling)), self.bs.resolve())

    def test_an_explicit_path_that_isnt_one_is_an_error_not_a_fallback(self) -> None:
        with self.assertRaises(fb.BuildSystemError):
            fb.find_build_system(str(self.cwd), workspace=[str(self.root)])

    def test_none_found_names_the_installer(self) -> None:
        with mock.patch.object(fb.Path, "home", return_value=self.cwd):
            res = result_of(run_engine(["build", "--workspace", str(self.cwd), "--port", "unix"]))
        self.assertFalse(res["ok"])
        self.assertIn(fb.INSTALL_COMMAND, res["error"])


class RealLayout(unittest.TestCase):
    def test_expected_build_dirs_match_build_mps_layout(self) -> None:
        bs = Path("/c")
        self.assertEqual(fb.expected_build_dir(bs, port="unix"), Path("/c/builds/unix/standard"))
        self.assertEqual(fb.expected_build_dir(bs, port="esp32", board="B"), Path("/c/builds/esp32/B/default"))
        self.assertEqual(fb.expected_build_dir(bs, port="esp32", board="B", variant="V"),
                         Path("/c/builds/esp32/B/V"))
        self.assertEqual(fb.expected_build_dir(bs, port="unix", interpreter="circuitpython"),
                         Path("/c/builds/circuitpython/unix/coverage"))
        self.assertEqual(fb.expected_build_dir(bs, port="raspberrypi", board="f", interpreter="circuitpython"),
                         Path("/c/builds/circuitpython/raspberrypi/f"))

    def test_engine_takes_make_args(self) -> None:
        ns = firmware.build_parser().parse_args(["build", "--port", "unix", "--make-arg=CIRCUITPY_ULAB=0"])
        self.assertIsInstance(ns, argparse.Namespace)
        self.assertEqual(firmware._make_args(ns), ["CIRCUITPY_ULAB=0"])


if __name__ == "__main__":
    unittest.main()
