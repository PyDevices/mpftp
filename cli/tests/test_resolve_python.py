"""Which interpreter the sidecar runs in."""

import io
import os
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from mpftp import cli

WIN = str(Path.home() / "bin" / "python.exe")


class ResolvePythonTests(unittest.TestCase):
    def _resolve(self, answers, wsl, device=None, env_python=None):
        """``answers`` maps a candidate to its probe result (None = usable)."""
        calls = []

        def probe(cand, timeout):
            calls.append((cand, timeout))
            ans = answers.get(cand, "not found")
            return ans(timeout) if callable(ans) else ans

        env = {k: v for k, v in os.environ.items() if not k.startswith("WSL_") and k != "MPFTP_PYTHON"}
        if wsl:
            env["WSL_DISTRO_NAME"] = "Ubuntu"
        if env_python:
            env["MPFTP_PYTHON"] = env_python
        err = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(cli.config, "resolve", return_value=None), \
                mock.patch.object(cli, "_probe_python", side_effect=probe), \
                redirect_stderr(err):
            try:
                return cli.resolve_python(device), calls, err.getvalue()
            except SystemExit as e:
                return None, calls, str(e)

    def test_wsl_prefers_windows_python(self):
        got, _, err = self._resolve({WIN: None, sys.executable: None}, wsl=True)
        self.assertEqual(got, WIN)
        self.assertEqual(err, "")

    def test_wsl_without_windows_python_uses_own_and_says_why(self):
        got, _, err = self._resolve({sys.executable: None}, wsl=True)
        self.assertEqual(got, sys.executable)
        self.assertIn("COM ports can't be opened", err)
        self.assertIn("python.exe: not found", err)

    def test_wsl_slow_windows_python_gets_a_longer_try(self):
        got, calls, _ = self._resolve(
            {WIN: lambda t: "no answer in 15 s" if t == 15 else None, sys.executable: None}, wsl=True
        )
        self.assertEqual(got, WIN)
        self.assertIn((WIN, 45), calls)

    def test_wsl_linux_device_runs_the_sidecar_in_linux(self):
        got, calls, err = self._resolve({WIN: None, sys.executable: None}, wsl=True, device="/dev/ttyACM0")
        self.assertEqual(got, sys.executable)
        self.assertNotIn(WIN, [c for c, _ in calls])
        self.assertEqual(err, "")

    def test_wsl_com_device_still_uses_windows_python(self):
        got, _, _ = self._resolve({WIN: None, sys.executable: None}, wsl=True, device="COM17")
        self.assertEqual(got, WIN)

    def test_mpftp_python_overrides_for_a_linux_device(self):
        got, _, _ = self._resolve({}, wsl=True, device="/dev/ttyACM0", env_python="/opt/py")
        self.assertEqual(got, "/opt/py")

    def test_elsewhere_the_running_interpreter_comes_first(self):
        got, calls, _ = self._resolve({sys.executable: None, "python.exe": None}, wsl=False)
        self.assertEqual(got, sys.executable)
        self.assertEqual(calls[0][0], sys.executable)

    def test_no_usable_python_names_each_reason(self):
        got, _, msg = self._resolve({}, wsl=True)
        self.assertIsNone(got)
        self.assertIn("no Python with mpremote and pyserial", msg)
        self.assertIn("python.exe: not found", msg)


class FakeClient:
    def __init__(self, python, ports):
        self.python, self.ports = python, ports

    def call(self, method):
        return list(self.ports)

    def close(self):
        pass


class PortsUnderWslTests(unittest.TestCase):
    def test_ports_adds_linux_ports_to_the_windows_list(self):
        win = [{"device": "COM31"}]
        lin = [{"device": "/dev/ttyACM0"}]

        def make(python):
            return FakeClient(python, lin if python == "linux-py" else win)

        def resolve(device=None):
            return "linux-py" if device == "/dev/" else "win-py"

        out = []
        with mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}), \
                mock.patch.object(cli, "resolve_python", side_effect=resolve), \
                mock.patch.object(cli, "get_client", return_value=(make("win-py"), "sidecar")), \
                mock.patch.object(cli, "SidecarClient", side_effect=make), \
                mock.patch.object(cli, "out", side_effect=out.append):
            cli.cmd_ports(None)
        self.assertEqual([p["device"] for p in out[0]], ["COM31", "/dev/ttyACM0"])


if __name__ == "__main__":
    unittest.main()
