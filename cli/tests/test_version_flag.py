"""``mpftp --version`` prints the version and exits 0 (mpftp#52)."""

from __future__ import annotations

import contextlib
import io
import unittest

from mpftp import __version__
from mpftp.cli import build_parser


class VersionFlagTest(unittest.TestCase):
    def test_prints_version_and_exits_zero(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            build_parser().parse_args(["--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f"mpftp {__version__}")


if __name__ == "__main__":
    unittest.main()
