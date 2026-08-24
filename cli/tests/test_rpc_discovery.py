"""CLI RPC address discovery: env override, then workspace registry, then nothing.

There is deliberately no third fallback. The extension only ever listens on an
ephemeral port while a board is connected, so there is no fixed port to probe
and no home-wide "last writer" file that could mean anything once more than
one window can be connected at once — a caller that finds nothing here falls
back to spawning its own private sidecar instead.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _load_cli():
    from mpftp import cli

    return cli


class RpcDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_cli()

    def test_env_override(self):
        prev = os.environ.get("MPFTP_RPC")
        os.environ["MPFTP_RPC"] = "127.0.0.1:7999"
        try:
            self.assertEqual(self.mod.find_rpc_addr(), ("127.0.0.1", 7999))
        finally:
            if prev is None:
                os.environ.pop("MPFTP_RPC", None)
            else:
                os.environ["MPFTP_RPC"] = prev

    def test_workspace_registry_match(self):
        prev = os.environ.pop("MPFTP_RPC", None)
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                ws = (root / "proj").resolve()
                ws.mkdir()
                home_mpftp = root / "home_mpftp"
                home_mpftp.mkdir()
                (home_mpftp / "workspace-rpc.json").write_text(
                    json.dumps({str(ws): "127.0.0.1:7501"}),
                    encoding="utf-8",
                )

                old_home = self.mod.HOME_MPFTP
                old_win = self.mod.WIN_MPFTP
                self.mod.HOME_MPFTP = home_mpftp
                self.mod.WIN_MPFTP = home_mpftp
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
                    self.mod.HOME_MPFTP = old_home
                    self.mod.WIN_MPFTP = old_win
        finally:
            if prev is not None:
                os.environ["MPFTP_RPC"] = prev

    def test_no_env_and_no_registry_match_returns_none(self):
        prev = os.environ.pop("MPFTP_RPC", None)
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                ws = (root / "proj").resolve()
                ws.mkdir()
                home_mpftp = root / "home_mpftp"
                home_mpftp.mkdir()
                # No workspace-rpc.json at all: no listener has ever run here.

                old_home = self.mod.HOME_MPFTP
                old_win = self.mod.WIN_MPFTP
                self.mod.HOME_MPFTP = home_mpftp
                self.mod.WIN_MPFTP = home_mpftp
                old_cwd = Path.cwd()
                try:
                    os.chdir(ws)
                    self.assertIsNone(self.mod.find_rpc_addr())
                finally:
                    os.chdir(old_cwd)
                    self.mod.HOME_MPFTP = old_home
                    self.mod.WIN_MPFTP = old_win
        finally:
            if prev is not None:
                os.environ["MPFTP_RPC"] = prev

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

if __name__ == "__main__":
    unittest.main()
