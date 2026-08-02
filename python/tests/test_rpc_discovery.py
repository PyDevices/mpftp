"""CLI RPC address discovery prefers workspace registry over home."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _load_cli():
    path = Path(__file__).resolve().parents[1] / "mpftp_cli.py"
    spec = importlib.util.spec_from_file_location("mpftp_cli", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


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

    def test_workspace_registry_beats_home(self):
        prev = os.environ.pop("MPFTP_RPC", None)
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                ws = (root / "proj").resolve()
                ws.mkdir()
                home_mpftp = root / "home_mpftp"
                home_mpftp.mkdir()
                (home_mpftp / "rpc.port").write_text("127.0.0.1:7429\n", encoding="utf-8")
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
