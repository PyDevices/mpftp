# SPDX-FileCopyrightText: 2026 Brad Barnett
#
# SPDX-License-Identifier: MIT

"""The sidecar the CLI starts runs the CLI's own mcuctl (mcuctl#171).

On WSL the CLI starts its sidecar under Windows Python, for the COM ports.
``python.exe -m mcuctl.sidecar`` imported whatever mcuctl that interpreter had
installed: an older release that wrote to the first CIRCUITPY drive it found.
So ``put`` to an ESP32-S3 behind a USB-UART bridge landed on another board's
drive, even though the CLI's own code would have refused that drive.

Here the "interpreter" is a wrapper whose installed mcuctl is such an older
release: its sidecar writes every ``fs_write`` into the first of two mounted
drives. The CLI must start its own sidecar instead, which, with no board
connected, writes nowhere.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from mcuctl import cli

# The sidecar of an older mcuctl release: every write goes to the first drive.
STALE_SIDECAR = textwrap.dedent(
    '''
    import base64, json, os, sys
    from pathlib import Path

    def send(msg):
        sys.stdout.write(json.dumps(msg) + "\\n")
        sys.stdout.flush()

    send({"type": "notify", "method": "ready", "params": {}})
    first_drive = Path(os.environ["STALE_FIRST_DRIVE"])
    for line in sys.stdin:
        req = json.loads(line)
        p = req.get("params") or {}
        if req["method"] == "fs_write":
            (first_drive / p["path"].lstrip("/")).write_bytes(base64.b64decode(p["data_b64"]))
            result = {"path": p["path"], "via": "circuitpy_msc"}
        elif req["method"] == "ping":
            result = {"pong": True, "stale": True}
        else:
            result = {}
        send({"type": "result", "id": req["id"], "result": result})
    '''
)


@unittest.skipIf(sys.platform == "win32", "the stand-in interpreter is a shell script")
class SidecarRunsThisMcuctlTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="mcuctl-171-")
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        # An interpreter whose own site-packages holds the older mcuctl.
        site = tmp / "site-packages" / "mcuctl"
        site.mkdir(parents=True)
        (site / "__init__.py").write_text("")
        (site / "sidecar.py").write_text(STALE_SIDECAR)
        self.python = tmp / "python"
        self.python.write_text(
            f'#!/bin/sh\nPYTHONPATH="{site.parent}" exec "{sys.executable}" "$@"\n'
        )
        self.python.chmod(self.python.stat().st_mode | stat.S_IXUSR)
        # Two other CircuitPython boards' drives, as H: and J:.
        self.drives = []
        for name, uid in (("H", "DF625857C74B5E2B"), ("J", "F412FA4B2C10")):
            d = tmp / name
            d.mkdir()
            (d / "boot_out.txt").write_text(f"Adafruit CircuitPython 9.2.1\nUID:{uid}\n")
            self.drives.append(d)
        self.home = tmp / "home"
        self.home.mkdir()

    def _client(self):
        env = {
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "MCUCTL_SESSION_ID": "test-171",
            "STALE_FIRST_DRIVE": str(self.drives[0]),
        }
        with mock.patch.dict(os.environ, env):
            client = cli.SidecarClient(str(self.python))
        self.addCleanup(client.close)
        return client

    def test_the_sidecar_is_the_clis_own(self):
        pong = self._client().call("ping")
        self.assertTrue(pong.get("pong"))
        self.assertNotIn("stale", pong, "the interpreter's installed mcuctl answered")

    def test_a_put_never_reaches_another_boards_drive(self):
        client = self._client()
        try:
            client.call("fs_write", {"path": "/settings.toml", "data_b64": "ZHVtbXkK"})
        except Exception:
            pass  # no board connected: the CLI's own sidecar refuses
        for d in self.drives:
            self.assertEqual(sorted(p.name for p in d.iterdir()), ["boot_out.txt"], d)


class OwnModuleArgvTests(unittest.TestCase):
    def test_it_loads_mcuctl_from_this_package(self):
        argv = cli.own_module_argv("/usr/bin/python3", "mcuctl.sidecar")
        self.assertEqual(argv[0], "/usr/bin/python3")
        self.assertEqual(argv[1], "-c")
        # Still "-m mcuctl.sidecar" to anything that finds the process by name.
        self.assertEqual(argv[3:5], ["-m", "mcuctl.sidecar"])
        self.assertEqual(Path(argv[5]), Path(cli.__file__).resolve().parent)

    def test_a_windows_python_gets_the_windows_spelling(self):
        with mock.patch.object(
            cli, "_wsl_path_for_windows_sidecar", return_value="\\\\wsl.localhost\\U\\mcuctl"
        ) as spell:
            argv = cli.own_module_argv("/home/u/bin/python.exe", "mcuctl.sidecar")
        spell.assert_called_once()
        self.assertEqual(argv[5], "\\\\wsl.localhost\\U\\mcuctl")


if __name__ == "__main__":
    unittest.main()
