"""WSLENV forwarding when spawning a Windows interpreter from WSL (mpftp#12).

A Windows binary spawned from WSL only receives env vars named in WSLENV
(with a translation flag) — a plain env dict passed to subprocess is not
enough. MICROPYPATH is the reported case: it silently falls back to the
Windows interpreter's own default lib path with no error.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from mpftp.cli import _is_windows_python, _wsl_path_for_windows_sidecar, _wslenv_forwarded_env


class IsWindowsPythonTests(unittest.TestCase):
    def test_exe_suffix_is_windows(self):
        self.assertTrue(_is_windows_python("python.exe"))

    def test_mnt_c_path_is_windows(self):
        self.assertTrue(_is_windows_python("/mnt/c/Users/bob/python.exe"))

    def test_linux_python_is_not_windows(self):
        self.assertFalse(_is_windows_python("/usr/bin/python3"))


class WslenvForwardedEnvTests(unittest.TestCase):
    def _wsl_env(self, **extra):
        env = {"WSL_DISTRO_NAME": "Ubuntu", "MICROPYPATH": "/home/x/lib"}
        env.update(extra)
        return env

    def test_adds_micropypath_for_a_windows_target_on_wsl(self):
        with mock.patch.dict(os.environ, self._wsl_env(), clear=True):
            env = _wslenv_forwarded_env("python.exe")
        self.assertIsNotNone(env)
        self.assertEqual(env["WSLENV"], "MICROPYPATH/l")

    def test_preserves_existing_wslenv_entries(self):
        with mock.patch.dict(os.environ, self._wsl_env(WSLENV="FOO/p"), clear=True):
            env = _wslenv_forwarded_env("python.exe")
        self.assertEqual(env["WSLENV"], "FOO/p:MICROPYPATH/l")

    def test_does_not_duplicate_an_existing_forward(self):
        with mock.patch.dict(os.environ, self._wsl_env(WSLENV="MICROPYPATH/l"), clear=True):
            env = _wslenv_forwarded_env("python.exe")
        self.assertIsNone(env)

    def test_none_for_a_linux_target(self):
        with mock.patch.dict(os.environ, self._wsl_env(), clear=True):
            env = _wslenv_forwarded_env("/usr/bin/python3")
        self.assertIsNone(env)

    def test_none_off_wsl(self):
        env_vars = {"MICROPYPATH": "/home/x/lib"}
        with mock.patch.dict(os.environ, env_vars, clear=True):
            env = _wslenv_forwarded_env("python.exe")
        self.assertIsNone(env)

    def test_none_when_micropypath_unset(self):
        with mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}, clear=True):
            env = _wslenv_forwarded_env("python.exe")
        self.assertIsNone(env)


class WslPathForWindowsSidecarTests(unittest.TestCase):
    def test_translates_via_wslpath_on_wsl(self):
        fake = mock.Mock(returncode=0, stdout="\\\\wsl.localhost\\Ubuntu\\tmp\\tee.log\n")
        with mock.patch.dict(
            os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}, clear=True
        ), mock.patch("mpftp.cli.subprocess.run", return_value=fake) as run:
            got = _wsl_path_for_windows_sidecar("/tmp/tee.log")
        run.assert_called_once_with(
            ["wslpath", "-w", "/tmp/tee.log"], capture_output=True, text=True, timeout=3
        )
        self.assertEqual(got, "\\\\wsl.localhost\\Ubuntu\\tmp\\tee.log")

    def test_falls_back_to_unc_form_when_wslpath_is_unavailable(self):
        with mock.patch.dict(
            os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}, clear=True
        ), mock.patch("mpftp.cli.subprocess.run", side_effect=FileNotFoundError):
            got = _wsl_path_for_windows_sidecar("/tmp/tee.log")
        self.assertEqual(got, "\\\\wsl.localhost\\Ubuntu\\tmp\\tee.log")

    def test_leaves_a_windows_path_unchanged(self):
        with mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}, clear=True):
            got = _wsl_path_for_windows_sidecar("C:\\tmp\\tee.log")
        self.assertEqual(got, "C:\\tmp\\tee.log")

    def test_leaves_path_unchanged_off_wsl(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            got = _wsl_path_for_windows_sidecar("/tmp/tee.log")
        self.assertEqual(got, "/tmp/tee.log")


if __name__ == "__main__":
    unittest.main()
