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


class WindowsSpellingTests(unittest.TestCase):
    """A recursive put from WSL hands the Windows sidecar a /mnt/<drive> path."""

    def test_mount_paths_get_a_drive_letter_on_windows(self):
        from mpftp import sidecar

        with mock.patch.object(sidecar.os, "name", "nt"):
            self.assertEqual(sidecar._windows_spelling("/mnt/c/Users/me/lib"), "C:\\Users\\me\\lib")
            self.assertEqual(sidecar._windows_spelling("/mnt/d"), "D:\\")
            self.assertIsNone(sidecar._windows_spelling("/home/me/lib"))
            self.assertIsNone(sidecar._windows_spelling("/mnt/cd/x"))

    def test_nothing_changes_off_windows(self):
        from mpftp import sidecar

        with mock.patch.object(sidecar.os, "name", "posix"):
            self.assertIsNone(sidecar._windows_spelling("/mnt/c/Users/me/lib"))


class PutIntoMissingParentTests(unittest.TestCase):
    """mpftp#185: a missing parent folder is named, not a file inside it."""

    def test_names_the_missing_folder(self):
        import tempfile
        from unittest import mock as _mock

        from mpftp import sidecar

        t = _mock.Mock()
        t.fs_mkdir.side_effect = OSError(2, "ENOENT")
        t.fs_stat.side_effect = OSError(2, "ENOENT")
        s = sidecar.Session.__new__(sidecar.Session)
        s._circuitpy_msc_root = lambda: None
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.py").write_text("v = 1\n")
            with self.assertRaises(RuntimeError) as cm:
                s._cp_local_to_remote(t, d, "/repro/cast", True, [], [])
        self.assertIn("/repro isn't on the board", str(cm.exception))
        t.fs_writefile.assert_not_called()


class _FakeBoard:
    """A dict-backed transport: just enough of the fs_* calls for folder copies."""

    def __init__(self, dirs=(), files=None):
        self.dirs = {"/", *dirs}
        self.files = dict(files or {})

    def fs_stat(self, path):
        path = path.rstrip("/") or "/"
        if path in self.dirs:
            return mock.Mock(st_mode=0x4000)
        if path in self.files:
            return mock.Mock(st_mode=0x8000)
        raise OSError(2, "ENOENT")

    def fs_mkdir(self, path):
        path = path.rstrip("/")
        if path in self.dirs or path in self.files:
            raise OSError(17, "EEXIST")
        if (path.rsplit("/", 1)[0] or "/") not in self.dirs:
            raise OSError(2, "ENOENT")
        self.dirs.add(path)

    def fs_writefile(self, path, data):
        self.files[path] = data

    def fs_hashfile(self, path, algo):
        import hashlib

        return hashlib.sha256(self.files[path]).digest()


def _session():
    from mpftp import sidecar

    s = sidecar.Session.__new__(sidecar.Session)
    s._circuitpy_msc_root = lambda: None
    s._sync_remote_fs = lambda t: None
    return s


class TrailingSlashTests(unittest.TestCase):
    """mpftp#185: `put -r src/ /dest` copies src's contents into /dest, rsync-style."""

    def _tree(self, d, files):
        for name, text in files.items():
            p = Path(d) / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)

    def test_trailing_slash_updates_existing_folder_in_place(self):
        board = _FakeBoard(dirs=["/lib", "/lib/sub"], files={"/lib/a.py": b"old", "/lib/stale.py": b"x"})
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "lib"
            self._tree(src, {"a.py": "new", "sub/b.py": "b"})
            copied = []
            _session()._cp_local_to_remote(board, str(src) + "/", "/lib", True, copied, [])
        self.assertEqual(board.files["/lib/a.py"], b"new")
        self.assertEqual(board.files["/lib/sub/b.py"], b"b")
        self.assertNotIn("/lib/lib", board.dirs)
        self.assertIn("/lib/stale.py", board.files)  # like rsync without --delete

    def test_no_trailing_slash_still_nests_into_existing_folder(self):
        board = _FakeBoard(dirs=["/lib"])
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "cast"
            self._tree(src, {"a.py": "new"})
            _session()._cp_local_to_remote(board, str(src), "/lib", True, [], [])
        self.assertIn("/lib/cast/a.py", board.files)

    def test_trailing_slash_creates_missing_destination(self):
        board = _FakeBoard()
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "cast"
            self._tree(src, {"a.py": "new"})
            _session()._cp_local_to_remote(board, str(src) + "/", "/cast", True, [], [])
        self.assertIn("/cast/a.py", board.files)

    def test_cli_keeps_the_trailing_slash(self):
        client = mock.Mock()
        client.call.return_value = {"ok": True}
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "dir"
            src.mkdir()
            for arg, want in ((str(src) + "/", True), (str(src), False)):
                client.reset_mock()
                with mock.patch("mpftp.cli.get_client", return_value=(client, "tcp")), mock.patch(
                    "mpftp.cli.ensure_device"
                ), redirect_stdout(io.StringIO()):
                    cmd_put(argparse.Namespace(
                        local=arg, remote="/d", recursive=True, mpy=False, verify=True,
                        device="COM1", baud=115200))
                got = client.call.call_args[0][1]["src"]
                self.assertEqual(got.endswith("/"), want)

    def test_get_trailing_slash_copies_contents_into_existing_folder(self):
        s = _session()
        s._board_listdir = lambda t, p: [__import__("types").SimpleNamespace(st_mode=0x8000, name="a.py")]
        t = _FakeBoard(dirs=["/lib"], files={"/lib/a.py": b"A"})
        t.fs_readfile = lambda p: t.files[p]
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "out").mkdir()
            s._cp_remote_to_local(t, "/lib/", str(Path(d) / "out"), True, [], [])
            self.assertEqual((Path(d) / "out" / "a.py").read_bytes(), b"A")
            s._cp_remote_to_local(t, "/lib", str(Path(d) / "out"), True, [], [])
            self.assertEqual((Path(d) / "out" / "lib" / "a.py").read_bytes(), b"A")
