"""Unit tests for mpftp.config: ~/.mpftp/config.json (no board needed)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from mpftp import config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._td = self.enterContext(
            mock.patch.object(config, "CONFIG_DIR", Path(self.mktemp_dir()))
        )
        self.addCleanup(mock.patch.stopall)

    def mktemp_dir(self) -> str:
        import tempfile

        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d

    def _config_path(self) -> Path:
        return config.CONFIG_DIR / "config.json"

    def test_defaults_when_file_missing(self):
        with mock.patch.object(config, "CONFIG_PATH", self._config_path()):
            values = config.load()
        self.assertEqual(values["defaultBaud"], 115200)
        self.assertTrue(values["verifyTransfers"])
        self.assertEqual(values["firmware"], {})

    def test_write_then_load_round_trips(self):
        with mock.patch.object(config, "CONFIG_PATH", self._config_path()):
            config.update({"defaultBaud": 921600, "workspacePath": "/boards"})
            values = config.load()
        self.assertEqual(values["defaultBaud"], 921600)
        self.assertEqual(values["workspacePath"], "/boards")

    def test_write_is_atomic_no_leftover_temp_files(self):
        with mock.patch.object(config, "CONFIG_PATH", self._config_path()):
            config.update({"defaultBaud": 9600})
        leftovers = list(config.CONFIG_DIR.glob(".config-*"))
        self.assertEqual(leftovers, [])

    def test_unknown_key_rejected(self):
        with (
            mock.patch.object(config, "CONFIG_PATH", self._config_path()),
            self.assertRaises(config.ConfigError),
        ):
            config.update({"totallyMadeUp": True})

    def test_wrong_type_rejected(self):
        with (
            mock.patch.object(config, "CONFIG_PATH", self._config_path()),
            self.assertRaises(config.ConfigError),
        ):
            config.update({"defaultBaud": "fast"})

    def test_corrupt_file_raises_on_read(self):
        path = self._config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        with (
            mock.patch.object(config, "CONFIG_PATH", path),
            self.assertRaises(config.ConfigError),
        ):
            config.read_file()

    def test_env_var_overrides_file(self):
        path = self._config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"defaultBaud": 921600}), encoding="utf-8")
        with (
            mock.patch.object(config, "CONFIG_PATH", path),
            mock.patch.dict("os.environ", {"MPFTP_BAUD": "9600"}),
        ):
            self.assertEqual(config.load()["defaultBaud"], 9600)

    def test_explicit_override_beats_everything(self):
        path = self._config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"defaultBaud": 921600}), encoding="utf-8")
        with (
            mock.patch.object(config, "CONFIG_PATH", path),
            mock.patch.dict("os.environ", {"MPFTP_BAUD": "9600"}),
        ):
            self.assertEqual(config.resolve("defaultBaud", 460800), 460800)

    def test_firmware_state_round_trips_and_merges(self):
        with mock.patch.object(config, "CONFIG_PATH", self._config_path()):
            config.save_firmware_state({"lastDevice": "COM4"})
            config.save_firmware_state({"lastSelection": {"board": "ESP32"}})
            state = config.load_firmware_state()
        self.assertEqual(state["lastDevice"], "COM4")
        self.assertEqual(state["lastSelection"], {"board": "ESP32"})

    def test_firmware_state_does_not_pollute_top_level_settings(self):
        with mock.patch.object(config, "CONFIG_PATH", self._config_path()):
            config.save_firmware_state({"lastDevice": "COM4"})
            values = config.load()
        self.assertEqual(values["defaultBaud"], 115200)
        self.assertEqual(values["firmware"]["lastDevice"], "COM4")


if __name__ == "__main__":
    unittest.main()
