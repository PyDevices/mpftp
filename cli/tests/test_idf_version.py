"""ESP-IDF version / docs URL parsing from ports/esp32/README.md."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mpftp.firmware import (
    idf_docs_url,
    idf_need_toolchain,
    recommended_idf_version,
    supported_idf_minors,
)

README = """\
# MicroPython ESP32

The ESP-IDF changes quickly and MicroPython only supports certain versions. The
current recommended version of ESP-IDF for MicroPython is v5.5.2. MicroPython
also supports v5.3, v5.4, v5.4.1, v5.4.2, v5.5.1 and v5.5.4.

To check out a copy of the IDF use git clone:

$ git clone -b v5.5.1 --recursive https://github.com/espressif/esp-idf.git
"""


class TestIdfVersionDocs(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.port = Path(self.tmp.name)
        (self.port / "README.md").write_text(README, encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_recommended(self) -> None:
        self.assertEqual(recommended_idf_version(self.port), "v5.5.2")

    def test_docs_url_versioned(self) -> None:
        self.assertEqual(
            idf_docs_url("v5.5.2"),
            "https://docs.espressif.com/projects/esp-idf/en/v5.5.2/esp32/get-started/",
        )

    def test_need_toolchain_uses_recommended(self) -> None:
        need = idf_need_toolchain(self.port)
        self.assertEqual(need["label"], "ESP-IDF v5.5.2")
        self.assertIn("/en/v5.5.2/", need["url"])
        self.assertNotIn("/en/latest/", need["url"])
        self.assertNotIn("/en/stable/", need["url"])
        self.assertIn("v5.5.2", need["hint"])

    def test_supported_minors(self) -> None:
        minors = supported_idf_minors(self.port)
        self.assertIn("5.5", minors)
        self.assertIn("5.3", minors)

    def test_fallback_clone_branch(self) -> None:
        slim = self.port / "README.md"
        slim.write_text(
            "Install with:\n$ git clone -b v5.4.2 --recursive https://github.com/espressif/esp-idf.git\n",
            encoding="utf-8",
        )
        self.assertEqual(recommended_idf_version(self.port), "v5.4.2")


if __name__ == "__main__":
    unittest.main()
