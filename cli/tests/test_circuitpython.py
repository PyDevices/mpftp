"""Unit tests for CircuitPython serial helpers (no board required)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


def _load_sidecar():
    from mpftp import sidecar

    return sidecar


class CircuitPythonHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_sidecar()

    def test_normalize_interpreter_name(self):
        self.assertEqual(self.mod.normalize_interpreter_name("micropython"), "micropython")
        self.assertEqual(self.mod.normalize_interpreter_name("circuitpython"), "circuitpython")
        self.assertEqual(self.mod.normalize_interpreter_name("CircuitPython"), "circuitpython")
        self.assertEqual(self.mod.normalize_interpreter_name("unknown"), "micropython")

    def test_enter_repl_prompt_english(self):
        banner = b"Press any key to enter the REPL. Use CTRL-D to reload.\r\n"
        self.assertTrue(self.mod.data_has_enter_repl_prompt(banner))
        self.assertFalse(self.mod.data_has_enter_repl_prompt(b">>> "))
        self.assertFalse(self.mod.data_has_enter_repl_prompt(b""))

    def test_enter_repl_prompt_localized_substring(self):
        self.assertTrue(
            self.mod.data_has_enter_repl_prompt(
                b"Presiona cualquier tecla para entrar al REPL. Usa CTRL-D para recargar.\r\n"
            )
        )

    def test_circup_boot_out_text(self):
        text = self.mod.circup_boot_out_text(
            cpy_version="9.2.1", board_id="adafruit_qualia_s3_rgb666"
        )
        self.assertIn("CircuitPython 9.2.1", text)
        self.assertIn("Board ID:adafruit_qualia_s3_rgb666", text)

    def test_build_circup_argv(self):
        argv = self.mod.build_circup_argv(
            circup_exe="circup",
            stage_path=r"C:\tmp\stage",
            packages=["adafruit_ticks", "adafruit_bus_device"],
            cpy_version="9.2.1",
            board_id="adafruit_feather_esp32s3",
            py=True,
        )
        self.assertEqual(argv[0], "circup")
        self.assertIn("--path", argv)
        self.assertIn(r"C:\tmp\stage", argv)
        self.assertIn("--cpy-version", argv)
        self.assertIn("9.2.1", argv)
        self.assertIn("--board-id", argv)
        self.assertIn("install", argv)
        self.assertIn("--py", argv)
        self.assertIn("adafruit_ticks", argv)

    def test_build_circup_web_argv(self):
        argv = self.mod.build_circup_web_argv(
            circup_exe="circup",
            host="192.168.1.50",
            password="secret",
            packages=["adafruit_ticks"],
            py=False,
        )
        self.assertEqual(
            argv,
            [
                "circup",
                "--host",
                "192.168.1.50",
                "--password",
                "secret",
                "install",
                "adafruit_ticks",
            ],
        )

    def test_map_circuitpy_remote_path(self):
        root = Path("/tmp/circuitpy-root")
        self.assertEqual(self.mod.map_circuitpy_remote_path(root, "/"), root)
        self.assertEqual(
            self.mod.map_circuitpy_remote_path(root, "/lib/path.py"),
            root / "lib" / "path.py",
        )
        self.assertEqual(
            self.mod.map_circuitpy_remote_path(root, "lib/../etc/passwd"),
            root / "lib" / "etc" / "passwd",
        )

    def test_circup_staging_layout(self):
        """Host staging dir shaped like a CIRCUITPY root (boot_out + lib/)."""
        with tempfile.TemporaryDirectory(prefix="mpftp-circup-test-") as stage:
            boot = Path(stage) / "boot_out.txt"
            boot.write_text(
                self.mod.circup_boot_out_text(cpy_version="10.0.0", board_id="test_board"),
                encoding="utf-8",
            )
            lib = Path(stage) / "lib"
            lib.mkdir()
            (lib / "adafruit_ticks.mpy").write_bytes(b"mpy")
            # Expected remote paths after serial put of lib contents → /lib
            expected = sorted("/lib/" + p.name for p in lib.iterdir())
            self.assertEqual(expected, ["/lib/adafruit_ticks.mpy"])
            self.assertTrue(boot.is_file())


if __name__ == "__main__":
    unittest.main()


class BootloaderCodeTests(unittest.TestCase):
    """``machine.bootloader()`` does not exist on CircuitPython.

    The MicroPython form used to be sent unconditionally. It is dispatched with
    ``exec_raw_no_follow``, whose result is never read, and the surrounding
    ``except`` swallowed the ImportError -- so the board stayed exactly where it
    was while the caller was told ``{"ok": true}``. Entering the bootloader is
    the gateway to every reflash, which made that silence expensive.
    """

    @classmethod
    def setUpClass(cls):
        cls.codes = _load_sidecar().Session._BOOTLOADER_CODE

    def test_micropython_uses_machine_bootloader(self):
        code = self.codes["micropython"]
        self.assertIn("machine.bootloader()", code)

    def test_circuitpython_never_touches_machine(self):
        code = self.codes["circuitpython"]
        self.assertNotIn("machine", code)
        self.assertIn("microcontroller.on_next_reset", code)
        self.assertIn("RunMode.BOOTLOADER", code)
        # Arming the mode does nothing on its own; the reset is what applies it.
        self.assertIn("microcontroller.reset()", code)

    def test_unknown_interpreter_has_no_entry(self):
        # bootloader() reports ok:false for anything not in this table rather
        # than guessing at a mechanism.
        self.assertNotIn("", self.codes)
        self.assertEqual({"micropython", "circuitpython"}, set(self.codes))
