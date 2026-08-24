"""Compile-on-upload via mpy-cross (mpftp#4). No board or real mpy-cross required."""

from __future__ import annotations

import unittest
from unittest import mock


def _load_sidecar():
    from mpftp import sidecar

    return sidecar


class FindMpyCrossTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_sidecar()

    def test_prefers_the_firmware_workspace_build(self):
        import os
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            built_dir = Path(td) / "mpy-cross" / "build"
            built_dir.mkdir(parents=True)
            built = built_dir / "mpy-cross"
            built.write_text("")
            with mock.patch(
                "mpftp.firmware.find_micropython", return_value=Path(td)
            ), mock.patch.object(self.mod.shutil, "which", return_value="/usr/bin/mpy-cross"):
                exe = self.mod.find_mpy_cross(workspace=td)
            self.assertEqual(exe, str(built))
            self.assertTrue(os.path.isabs(exe))

    def test_falls_back_to_path(self):
        with mock.patch("mpftp.firmware.find_micropython", return_value=None), mock.patch.object(
            self.mod.shutil, "which", return_value="/usr/bin/mpy-cross"
        ):
            exe = self.mod.find_mpy_cross()
        self.assertEqual(exe, "/usr/bin/mpy-cross")

    def test_raises_a_clear_error_when_nothing_is_found(self):
        with mock.patch("mpftp.firmware.find_micropython", return_value=None), mock.patch.object(
            self.mod.shutil, "which", return_value=None
        ), self.assertRaises(RuntimeError) as ctx:
            self.mod.find_mpy_cross()
        self.assertIn("mpy-cross not found", str(ctx.exception))


class MpyCrossVersionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_sidecar()

    def test_parses_the_version_banner(self):
        proc = mock.Mock(stdout="MicroPython v1.24.0 on 2024-11-29; mpy-cross emitting mpy v6.3\n", stderr="")
        with mock.patch.object(self.mod.subprocess, "run", return_value=proc):
            self.assertEqual(self.mod.mpy_cross_version("mpy-cross"), (6, 3))

    def test_unparseable_output_is_a_clear_error(self):
        proc = mock.Mock(stdout="", stderr="not mpy-cross at all")
        with mock.patch.object(self.mod.subprocess, "run", return_value=proc), self.assertRaises(
            RuntimeError
        ) as ctx:
            self.mod.mpy_cross_version("mpy-cross")
        self.assertIn("could not determine mpy-cross version", str(ctx.exception))


class ReplaceRemoteBasenameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_sidecar()

    def test_swaps_basename_keeping_the_directory(self):
        self.assertEqual(self.mod._replace_remote_basename("/lib/a.py", "a.mpy"), "/lib/a.mpy")

    def test_bare_filename_has_no_directory_to_keep(self):
        self.assertEqual(self.mod._replace_remote_basename("a.py", "a.mpy"), "a.mpy")


class BoardMpyVersionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_sidecar()

    def test_reads_the_low_byte_of_sys_implementation_mpy(self):
        session = self.mod.Session()
        t = mock.Mock()
        t.eval.return_value = 6
        self.assertEqual(session._board_mpy_version(t), 6)

    def test_a_board_without_mpy_attribute_reports_zero(self):
        session = self.mod.Session()
        t = mock.Mock()
        t.eval.side_effect = RuntimeError("no attribute")
        self.assertEqual(session._board_mpy_version(t), 0)


class PrepareMpyCompileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_sidecar()

    def test_rejects_circuitpython_before_touching_mpy_cross(self):
        session = self.mod.Session()
        session.interpreter = "circuitpython"
        with self.assertRaises(RuntimeError) as ctx:
            session._prepare_mpy_compile(mock.Mock())
        self.assertIn("MicroPython-only", str(ctx.exception))

    def test_matching_versions_return_the_exe(self):
        session = self.mod.Session()
        session.interpreter = "micropython"
        with mock.patch.object(
            self.mod, "find_mpy_cross", return_value="/ws/mpy-cross"
        ), mock.patch.object(self.mod, "mpy_cross_version", return_value=(6, 3)), mock.patch.object(
            session, "_board_mpy_version", return_value=6
        ), mock.patch.object(self.mod.config, "resolve", return_value=""):
            exe = session._prepare_mpy_compile(mock.Mock())
        self.assertEqual(exe, "/ws/mpy-cross")

    def test_a_version_mismatch_is_a_clear_error_not_a_silent_upload(self):
        session = self.mod.Session()
        session.interpreter = "micropython"
        with mock.patch.object(
            self.mod, "find_mpy_cross", return_value="/ws/mpy-cross"
        ), mock.patch.object(self.mod, "mpy_cross_version", return_value=(6, 3)), mock.patch.object(
            session, "_board_mpy_version", return_value=5
        ), mock.patch.object(self.mod.config, "resolve", return_value=""), self.assertRaises(
            RuntimeError
        ) as ctx:
            session._prepare_mpy_compile(mock.Mock())
        self.assertIn("mpy v6.3", str(ctx.exception))
        self.assertIn("mpy v5", str(ctx.exception))

    def test_a_board_that_does_not_report_its_mpy_version_is_not_blocked(self):
        session = self.mod.Session()
        session.interpreter = "micropython"
        with mock.patch.object(
            self.mod, "find_mpy_cross", return_value="/ws/mpy-cross"
        ), mock.patch.object(self.mod, "mpy_cross_version", return_value=(6, 3)), mock.patch.object(
            session, "_board_mpy_version", return_value=0
        ), mock.patch.object(self.mod.config, "resolve", return_value=""):
            exe = session._prepare_mpy_compile(mock.Mock())
        self.assertEqual(exe, "/ws/mpy-cross")


class CompileToMpyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_sidecar()

    def test_invokes_mpy_cross_and_reads_back_the_output(self):
        session = self.mod.Session()

        def fake_run(argv, **kw):
            out_path = argv[argv.index("-o") + 1]
            with open(out_path, "wb") as f:
                f.write(b"\xfdcompiled")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(self.mod.subprocess, "run", side_effect=fake_run):
            name, data = session._compile_to_mpy("mpy-cross", "a.py", b"print(1)\n")
        self.assertEqual(name, "a.mpy")
        self.assertEqual(data, b"\xfdcompiled")

    def test_a_compile_failure_raises_with_the_mpy_cross_stderr(self):
        session = self.mod.Session()
        proc = mock.Mock(returncode=1, stdout="", stderr="a.py:1: SyntaxError")
        with mock.patch.object(self.mod.subprocess, "run", return_value=proc), self.assertRaises(
            RuntimeError
        ) as ctx:
            session._compile_to_mpy("mpy-cross", "a.py", b"def(:\n")
        self.assertIn("SyntaxError", str(ctx.exception))


class FsWriteCompileOnUploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_sidecar()

    def _session_with_fake_raw(self, t):
        session = self.mod.Session()
        session.interpreter = "micropython"
        session.with_raw = lambda fn, soft_reset=False: fn(t)
        session._ensure_cp_writable = lambda t: None
        session._sync_remote_fs = lambda t: None
        return session

    def test_compiles_py_files_and_writes_the_mpy_extension(self):
        t = mock.Mock()
        session = self._session_with_fake_raw(t)
        with mock.patch.object(
            session, "_prepare_mpy_compile", return_value="mpy-cross"
        ), mock.patch.object(
            session, "_compile_to_mpy", return_value=("a.mpy", b"COMPILED")
        ):
            result = session.fs_write("/a.py", self.mod.base64.b64encode(b"print(1)").decode(), mpy=True)
        t.fs_writefile.assert_called_once_with("/a.mpy", b"COMPILED")
        self.assertEqual(result["path"], "/a.mpy")
        self.assertTrue(result["compiled"])

    def test_boot_py_is_never_compiled_even_when_mpy_is_requested(self):
        t = mock.Mock()
        session = self._session_with_fake_raw(t)
        with mock.patch.object(self.mod.config, "resolve", return_value=["boot.py", "main.py"]):
            result = session.fs_write("/boot.py", self.mod.base64.b64encode(b"pass").decode(), mpy=True)
        t.fs_writefile.assert_called_once_with("/boot.py", b"pass")
        self.assertFalse(result["compiled"])

    def test_mpy_against_circuitpython_msc_is_a_clear_error(self):
        session = self.mod.Session()
        session._circuitpy_host_path = lambda path: mock.Mock()
        with self.assertRaises(RuntimeError) as ctx:
            session.fs_write("/a.py", self.mod.base64.b64encode(b"print(1)").decode(), mpy=True)
        self.assertIn("MicroPython-only", str(ctx.exception))

    def test_verify_hashes_the_compiled_bytes_not_the_source(self):
        t = mock.Mock()
        t.fs_hashfile.return_value = self.mod.host_sha256(b"COMPILED")
        session = self._session_with_fake_raw(t)
        with mock.patch.object(
            session, "_prepare_mpy_compile", return_value="mpy-cross"
        ), mock.patch.object(
            session, "_compile_to_mpy", return_value=("a.mpy", b"COMPILED")
        ):
            result = session.fs_write(
                "/a.py", self.mod.base64.b64encode(b"print(1)").decode(), mpy=True, verify=True
            )
        self.assertEqual(result["verified"], self.mod.host_sha256(b"COMPILED"))


class CpLocalToRemoteCompileOnUploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_sidecar()

    def test_a_directory_copy_compiles_py_files_but_skips_the_excluded_ones(self):
        import os
        import tempfile

        session = self.mod.Session()
        session.interpreter = "micropython"
        session._circuitpy_msc_root = lambda: None
        session._remote_isdir = lambda t, path: False

        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "app"))
            with open(os.path.join(td, "app", "lib.py"), "w") as f:
                f.write("print(1)\n")
            with open(os.path.join(td, "app", "main.py"), "w") as f:
                f.write("print(2)\n")

            t = mock.Mock()
            copied: list[str] = []
            verified: list[str] = []
            with mock.patch.object(
                session, "_prepare_mpy_compile", return_value="mpy-cross"
            ), mock.patch.object(
                session,
                "_compile_to_mpy",
                side_effect=lambda exe, name, data: (name.replace(".py", ".mpy"), b"COMPILED"),
            ), mock.patch.object(self.mod.config, "resolve", return_value=["boot.py", "main.py"]):
                session._cp_local_to_remote(
                    t, os.path.join(td, "app"), "/app", False, copied, verified, mpy=True
                )

        remote_names = sorted(call.args[0] for call in t.fs_writefile.call_args_list)
        self.assertIn("/app/lib.mpy", remote_names)
        self.assertIn("/app/main.py", remote_names)
        self.assertNotIn("/app/main.mpy", remote_names)


if __name__ == "__main__":
    unittest.main()
