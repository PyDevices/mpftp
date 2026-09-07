"""put: directories go through fs_cp, not Path.read_bytes (IsADirectoryError)."""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from mpftp.cli import cmd_put


class CmdPutTests(unittest.TestCase):
    def _ns(self, local: str, remote: str = "/dest", **overrides):
        base = dict(
            local=local,
            remote=remote,
            recursive=False,
            mpy=False,
            verify=True,
            device="COM1",
            baud=115200,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_put_r_directory_calls_fs_cp_instead_of_reading_bytes(self):
        client = mock.Mock()
        client.call.return_value = {"ok": True, "files": 1, "copied": ["a.py"]}
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "dir"
            src.mkdir()
            (src / "a.py").write_text("A = 1\n", encoding="utf-8")
            with mock.patch("mpftp.cli.get_client", return_value=(client, "tcp")), mock.patch(
                "mpftp.cli.ensure_device"
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    cmd_put(self._ns(str(src), "/mpftp_cli_test/subdir", recursive=True))
        result = json.loads(buf.getvalue())
        self.assertEqual(result["files"], 1)
        client.call.assert_called_once()
        method, params = client.call.call_args[0][0], client.call.call_args[0][1]
        self.assertEqual(method, "fs_cp")
        self.assertEqual(params["src"], str(src.resolve()))
        self.assertEqual(params["dest"], ":/mpftp_cli_test/subdir")

    def test_put_directory_without_dash_r_still_uses_fs_cp(self):
        client = mock.Mock()
        client.call.return_value = {"ok": True, "files": 1}
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "dir"
            src.mkdir()
            (src / "a.py").write_text("A = 1\n", encoding="utf-8")
            with mock.patch("mpftp.cli.get_client", return_value=(client, "tcp")), mock.patch(
                "mpftp.cli.ensure_device"
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    cmd_put(self._ns(str(src), "/dest"))
        method = client.call.call_args[0][0]
        self.assertEqual(method, "fs_cp")

    def test_put_file_still_uses_fs_write(self):
        client = mock.Mock()
        client.call.return_value = {"path": "/hello.txt", "size": 5}
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "hello.txt"
            src.write_text("hello", encoding="utf-8")
            with mock.patch("mpftp.cli.get_client", return_value=(client, "tcp")), mock.patch(
                "mpftp.cli.ensure_device"
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    cmd_put(self._ns(str(src), "/hello.txt", verify=False))
        method = client.call.call_args[0][0]
        self.assertEqual(method, "fs_write")


if __name__ == "__main__":
    unittest.main()
