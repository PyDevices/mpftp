"""``python -m mpftp``: no args launches the PWA, any args go to the CLI (mpftp#11 phase 11)."""

from __future__ import annotations

import unittest
from unittest import mock


def _load_dunder_main():
    from mpftp import __main__

    return __main__


class DunderMainDispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_dunder_main()

    def test_no_arguments_launches_the_pwa(self):
        with mock.patch("sys.argv", ["mpftp"]), mock.patch("mpftp.pwa.main") as pwa_main, mock.patch(
            "mpftp.cli.main"
        ) as cli_main:
            self.mod.main()
        pwa_main.assert_called_once_with([])
        cli_main.assert_not_called()

    def test_any_argument_goes_to_the_cli_unchanged(self):
        with mock.patch("sys.argv", ["mpftp", "ports"]), mock.patch(
            "mpftp.pwa.main"
        ) as pwa_main, mock.patch("mpftp.cli.main") as cli_main:
            self.mod.main()
        cli_main.assert_called_once_with()
        pwa_main.assert_not_called()


if __name__ == "__main__":
    unittest.main()
