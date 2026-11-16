# SPDX-FileCopyrightText: 2026 Brad Barnett
#
# SPDX-License-Identifier: MIT

"""CLI RPC address discovery: env override, then workspace registry, then nothing.

There is deliberately no third fallback. The extension only ever listens on an
ephemeral port while a board is connected, so there is no fixed port to probe
and no home-wide "last writer" file that could mean anything once more than
one window can be connected at once — a caller that finds nothing here falls
back to spawning its own private sidecar instead.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


def _load_cli():
    from mcuctl import cli

    return cli


class RpcDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_cli()

    def test_env_override(self):
        prev = os.environ.get("MCUCTL_RPC")
        os.environ["MCUCTL_RPC"] = "127.0.0.1:7999"
        try:
            self.assertEqual(self.mod.find_rpc_addr(), ("127.0.0.1", 7999))
        finally:
            if prev is None:
                os.environ.pop("MCUCTL_RPC", None)
            else:
                os.environ["MCUCTL_RPC"] = prev

    def test_workspace_registry_match(self):
        prev = os.environ.pop("MCUCTL_RPC", None)
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                ws = (root / "proj").resolve()
                ws.mkdir()
                home_mcuctl = root / "home_mcuctl"
                home_mcuctl.mkdir()
                (home_mcuctl / "workspace-rpc.json").write_text(
                    json.dumps({str(ws): "127.0.0.1:7501"}),
                    encoding="utf-8",
                )

                old_home = self.mod.HOME_MCUCTL
                old_win = self.mod.WIN_MCUCTL
                self.mod.HOME_MCUCTL = home_mcuctl
                self.mod.WIN_MCUCTL = home_mcuctl
                old_cwd = Path.cwd()
                try:
                    os.chdir(ws)
                    self.assertEqual(self.mod.find_rpc_addr(), ("127.0.0.1", 7501))
                    # Nested cwd still matches workspace root.
                    nested = ws / "src" / "pkg"
                    nested.mkdir(parents=True)
                    os.chdir(nested)
                    self.assertEqual(self.mod.find_rpc_addr(), ("127.0.0.1", 7501))
                finally:
                    os.chdir(old_cwd)
                    self.mod.HOME_MCUCTL = old_home
                    self.mod.WIN_MCUCTL = old_win
        finally:
            if prev is not None:
                os.environ["MCUCTL_RPC"] = prev

    def test_no_env_and_no_registry_match_returns_none(self):
        prev = os.environ.pop("MCUCTL_RPC", None)
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                ws = (root / "proj").resolve()
                ws.mkdir()
                home_mcuctl = root / "home_mcuctl"
                home_mcuctl.mkdir()
                # No workspace-rpc.json at all: no listener has ever run here.

                old_home = self.mod.HOME_MCUCTL
                old_win = self.mod.WIN_MCUCTL
                self.mod.HOME_MCUCTL = home_mcuctl
                self.mod.WIN_MCUCTL = home_mcuctl
                old_cwd = Path.cwd()
                try:
                    os.chdir(ws)
                    self.assertIsNone(self.mod.find_rpc_addr())
                finally:
                    os.chdir(old_cwd)
                    self.mod.HOME_MCUCTL = old_home
                    self.mod.WIN_MCUCTL = old_win
        finally:
            if prev is not None:
                os.environ["MCUCTL_RPC"] = prev

    def test_object_shaped_registry_entry_surfaces_editor_and_pid(self):
        """mcuctl#21: two editors sharing a workspace root need to be
        distinguishable, not just "a session exists at this address"."""
        prev = os.environ.pop("MCUCTL_RPC", None)
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                ws = (root / "proj").resolve()
                ws.mkdir()
                home_mcuctl = root / "home_mcuctl"
                home_mcuctl.mkdir()
                (home_mcuctl / "workspace-rpc.json").write_text(
                    json.dumps(
                        {
                            str(ws): {
                                "addr": "127.0.0.1:7501",
                                "editor": "Cursor",
                                "pid": 4242,
                                "updatedAt": "2026-08-24T20:00:00.000Z",
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                old_home, old_win = self.mod.HOME_MCUCTL, self.mod.WIN_MCUCTL
                self.mod.HOME_MCUCTL = self.mod.WIN_MCUCTL = home_mcuctl
                old_cwd = Path.cwd()
                try:
                    os.chdir(ws)
                    self.assertEqual(self.mod.find_rpc_addr(), ("127.0.0.1", 7501))
                    entry = self.mod.find_rpc_entry()
                    self.assertEqual(entry["editor"], "Cursor")
                    self.assertEqual(entry["pid"], 4242)
                    self.assertEqual(entry["updatedAt"], "2026-08-24T20:00:00.000Z")
                finally:
                    os.chdir(old_cwd)
                    self.mod.HOME_MCUCTL, self.mod.WIN_MCUCTL = old_home, old_win
        finally:
            if prev is not None:
                os.environ["MCUCTL_RPC"] = prev

    def test_legacy_bare_string_entry_still_resolves_with_no_diagnostics(self):
        prev = os.environ.pop("MCUCTL_RPC", None)
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                ws = (root / "proj").resolve()
                ws.mkdir()
                home_mcuctl = root / "home_mcuctl"
                home_mcuctl.mkdir()
                (home_mcuctl / "workspace-rpc.json").write_text(
                    json.dumps({str(ws): "127.0.0.1:7502"}), encoding="utf-8"
                )
                old_home, old_win = self.mod.HOME_MCUCTL, self.mod.WIN_MCUCTL
                self.mod.HOME_MCUCTL = self.mod.WIN_MCUCTL = home_mcuctl
                old_cwd = Path.cwd()
                try:
                    os.chdir(ws)
                    entry = self.mod.find_rpc_entry()
                    self.assertEqual(entry, {"addr": "127.0.0.1:7502"})
                    self.assertEqual(self.mod.find_rpc_addr(), ("127.0.0.1", 7502))
                finally:
                    os.chdir(old_cwd)
                    self.mod.HOME_MCUCTL, self.mod.WIN_MCUCTL = old_home, old_win
        finally:
            if prev is not None:
                os.environ["MCUCTL_RPC"] = prev

    def test_dead_extension_sidecar_falls_back_to_standalone(self):
        tcp = mock.Mock()
        tcp.call.side_effect = ConnectionRefusedError(111, "Connection refused")
        sidecar = mock.Mock()
        with (
            mock.patch.object(self.mod, "find_rpc_addr", return_value=("127.0.0.1", 7429)),
            mock.patch.object(self.mod, "TcpClient", return_value=tcp),
            mock.patch.object(self.mod, "SidecarClient", return_value=sidecar),
            mock.patch.object(self.mod, "resolve_python", return_value="python.exe"),
        ):
            client, mode = self.mod.get_client()
        self.assertIs(client, sidecar)
        self.assertEqual(mode, "sidecar")
        tcp.call.assert_called_once_with("ping")

    def test_live_extension_sidecar_remains_preferred(self):
        tcp = mock.Mock()
        tcp.call.return_value = {"ok": True}
        with (
            mock.patch.object(self.mod, "find_rpc_addr", return_value=("127.0.0.1", 7429)),
            mock.patch.object(self.mod, "TcpClient", return_value=tcp),
            mock.patch.object(self.mod, "SidecarClient") as sidecar_cls,
        ):
            client, mode = self.mod.get_client()
        self.assertIs(client, tcp)
        self.assertEqual(mode, "tcp:127.0.0.1:7429")
        sidecar_cls.assert_not_called()

    def test_firmware_discovery_receives_cwd_ancestors(self):
        ns = mock.Mock(mp=None)
        with mock.patch.object(
            self.mod,
            "_engine_json",
            return_value={"micropython": "/workspace/micropython"},
        ) as engine:
            found = self.mod._resolve_mp(ns)
        self.assertEqual(found, "/workspace/micropython")
        command, args = engine.call_args.args
        self.assertEqual(command, "discover")
        self.assertEqual(args[0], "--workspace")
        hints = args[1].split(os.pathsep)
        self.assertEqual(Path(hints[0]), Path.cwd().resolve())
        self.assertIn(str(Path.cwd().resolve().parent), hints)

    def test_status_surfaces_which_editor_the_resolved_session_belongs_to(self):
        tcp = mock.Mock()
        tcp.call.return_value = {"connected": False}
        entry = {"addr": "127.0.0.1:7501", "editor": "Cursor", "pid": 4242}
        with (
            mock.patch.object(self.mod, "find_rpc_entry", return_value=entry),
            mock.patch.object(self.mod, "TcpClient", return_value=tcp),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.mod.cmd_status(argparse.Namespace())
        result = json.loads(buf.getvalue())
        self.assertEqual(result["rpc"], "127.0.0.1:7501")
        self.assertEqual(result["editor"], "Cursor")
        self.assertEqual(result["pid"], 4242)

    def test_status_with_no_live_session_has_no_editor_pid_fields(self):
        with mock.patch.object(self.mod, "find_rpc_entry", return_value=None):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.mod.cmd_status(argparse.Namespace())
        result = json.loads(buf.getvalue())
        self.assertIsNone(result["rpc"])
        self.assertNotIn("editor", result)
        self.assertNotIn("pid", result)


if __name__ == "__main__":
    unittest.main()
