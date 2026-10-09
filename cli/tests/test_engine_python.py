"""The firmware engine runs under the python that runs the CLI.

Picking the first ``python3`` on PATH found the system's python, which has no
mpftp when mpftp lives in a venv, so every ``mpftp firmware`` command failed
with "No module named mpftp.firmware". No board required.
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
from unittest import mock

from mpftp import cli


class EnginePythonTests(unittest.TestCase):
    def test_prefers_the_python_running_the_cli(self):
        env = {k: v for k, v in os.environ.items() if k != "MPFTP_BUILD_PYTHON"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            shutil, "which", return_value="/usr/bin/python3"
        ), mock.patch.object(sys, "executable", "/opt/venv/bin/python"):
            self.assertEqual(cli.resolve_build_python(), "/opt/venv/bin/python")

    def test_the_setting_still_wins(self):
        with mock.patch.dict(os.environ, {"MPFTP_BUILD_PYTHON": "/x/python"}):
            self.assertEqual(cli.resolve_build_python(), "/x/python")

    def test_engine_argv_starts_with_that_python(self):
        with mock.patch.object(cli, "resolve_build_python", return_value="/p"):
            self.assertEqual(
                cli._engine_argv("detect", ["--device", "COM4"]),
                ["/p", "-m", "mpftp.firmware", "detect", "--device", "COM4"],
            )


if __name__ == "__main__":
    unittest.main()
