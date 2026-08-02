"""CLI RPC address discovery prefers workspace registry over home."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


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

if __name__ == "__main__":
    unittest.main()
