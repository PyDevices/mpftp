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

from mpftp.cli import (
    _WSL_INTEROP_FALLBACK,
    _is_windows_python,
    _live_wsl_interop,
    _sidecar_died_message,
    _wsl_path_for_windows_sidecar,
    _wslenv_forwarded_env,
)


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

    def test_adds_pythonpath_as_a_path_list(self):
        env_vars = {
            "WSL_DISTRO_NAME": "Ubuntu",
            "PYTHONPATH": "/home/x/mpftp/cli/src",
        }
        with mock.patch.dict(os.environ, env_vars, clear=True):
            env = _wslenv_forwarded_env("python.exe")
        self.assertIsNotNone(env)
        self.assertEqual(env["WSLENV"], "PYTHONPATH/p")

    def test_forwards_both_micropypath_and_pythonpath(self):
        env_vars = {
            "WSL_DISTRO_NAME": "Ubuntu",
            "MICROPYPATH": "/home/x/lib",
            "PYTHONPATH": "/home/x/mpftp/cli/src",
        }
        with mock.patch.dict(os.environ, env_vars, clear=True):
            env = _wslenv_forwarded_env("python.exe")
        self.assertEqual(env["WSLENV"], "MICROPYPATH/l:PYTHONPATH/p")

    def test_none_when_nothing_to_forward(self):
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


class StaleWslInteropTests(unittest.TestCase):
    """A shell's WSL_INTEROP socket dies with its owner (mpftp#28).

    Every Windows .exe launch then times out on accept4 with errno 110, the
    sidecar dies before ``ready``, and the old message sent the reader to the
    serial port -- which was fine the whole time.
    """

    def test_a_live_socket_is_left_alone(self):
        with (
            mock.patch.dict(os.environ, {"WSL_INTEROP": "/run/WSL/99_interop"}, clear=True),
            mock.patch("os.path.exists", return_value=True),
        ):
            self.assertIsNone(_live_wsl_interop())

    def test_a_dead_socket_falls_back_to_inits(self):
        with (
            mock.patch.dict(os.environ, {"WSL_INTEROP": "/run/WSL/99_interop"}, clear=True),
            mock.patch("os.path.exists", lambda p: p == _WSL_INTEROP_FALLBACK),
        ):
            self.assertEqual(_live_wsl_interop(), _WSL_INTEROP_FALLBACK)

    def test_no_fallback_when_init_socket_is_absent_too(self):
        with (
            mock.patch.dict(os.environ, {"WSL_INTEROP": "/run/WSL/99_interop"}, clear=True),
            mock.patch("os.path.exists", return_value=False),
        ):
            self.assertIsNone(_live_wsl_interop())

    def test_nothing_to_do_off_wsl(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(_live_wsl_interop())

    def test_spawn_env_substitutes_the_live_socket(self):
        env = {"WSL_DISTRO_NAME": "Ubuntu", "WSL_INTEROP": "/run/WSL/99_interop"}
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch("os.path.exists", lambda p: p == _WSL_INTEROP_FALLBACK),
        ):
            spawned = _wslenv_forwarded_env("python.exe")
        self.assertIsNotNone(spawned)
        self.assertEqual(spawned["WSL_INTEROP"], _WSL_INTEROP_FALLBACK)

    def test_spawn_env_still_returns_none_when_everything_is_healthy(self):
        env = {"WSL_DISTRO_NAME": "Ubuntu", "WSL_INTEROP": "/run/WSL/99_interop"}
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch("os.path.exists", return_value=True),
        ):
            self.assertIsNone(_wslenv_forwarded_env("python.exe"))


class SidecarDiedMessageTests(unittest.TestCase):
    def test_vsock_failure_is_named_and_not_blamed_on_the_port(self):
        stderr = "<3>WSL (387453 - ) ERROR: UtilAcceptVsock:280: accept4 failed 110"
        message = _sidecar_died_message(stderr)
        self.assertIn("WSL interop", message)
        self.assertIn(_WSL_INTEROP_FALLBACK, message)
        self.assertIn("not the serial port", message)

    def test_accept4_alone_is_enough_to_recognise_it(self):
        self.assertIn("WSL interop", _sidecar_died_message("accept4 failed 110"))

    def test_any_other_failure_keeps_the_old_message(self):
        message = _sidecar_died_message("ImportError: no module named serial")
        self.assertTrue(message.startswith("sidecar exited early:"))
        self.assertNotIn("WSL interop", message)
