"""Per-window sidecar session pid isolation (no board required)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _load_sidecar():
    from mpftp import sidecar

    return sidecar


class SessionSidecarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_sidecar()

    def test_sanitize_session_id(self):
        self.assertEqual(self.mod.sanitize_session_id("abc/def:ghi"), "abc_def_ghi")
        self.assertEqual(self.mod.sanitize_session_id(""), "session")
        self.assertEqual(self.mod.sanitize_session_id("a" * 200)[:120], "a" * 120)

    def test_resolve_session_id_from_env(self):
        prev = os.environ.get("MPFTP_SESSION_ID")
        os.environ["MPFTP_SESSION_ID"] = "Window/One"
        try:
            self.assertEqual(self.mod.resolve_session_id(), "Window_One")
        finally:
            if prev is None:
                os.environ.pop("MPFTP_SESSION_ID", None)
            else:
                os.environ["MPFTP_SESSION_ID"] = prev

    def test_cleanup_does_not_kill_live_foreign_session(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sessions = root / "sessions"
            sessions.mkdir()
            foreign = sessions / "other-window.pid"
            foreign.write_text("424242\n", encoding="utf-8")
            legacy = root / "sidecar.pid"

            killed_pids: list[int] = []

            def fake_alive(pid: int) -> bool:
                return pid == 424242

            def fake_kill(pid: int) -> None:
                killed_pids.append(pid)

            with mock.patch.object(self.mod, "_mpftp_dir", return_value=root), mock.patch.object(
                self.mod, "_pid_alive", side_effect=fake_alive
            ), mock.patch.object(self.mod, "_force_kill_pid", side_effect=fake_kill):
                os.environ["MPFTP_SESSION_ID"] = "this-window"
                try:
                    killed = self.mod.cleanup_stale_sidecars("this-window")
                finally:
                    os.environ.pop("MPFTP_SESSION_ID", None)

            self.assertEqual(killed, [])
            self.assertNotIn(424242, killed_pids)
            self.assertTrue(foreign.exists())
            self.assertFalse(legacy.exists())

    def test_cleanup_kills_same_session_orphan(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sessions = root / "sessions"
            sessions.mkdir()
            mine = sessions / "this-window.pid"
            mine.write_text("111\n", encoding="utf-8")

            killed_pids: list[int] = []

            def fake_alive(pid: int) -> bool:
                return pid == 111

            def fake_kill(pid: int) -> None:
                killed_pids.append(pid)

            with mock.patch.object(self.mod, "_mpftp_dir", return_value=root), mock.patch.object(
                self.mod, "_pid_alive", side_effect=fake_alive
            ), mock.patch.object(self.mod, "_force_kill_pid", side_effect=fake_kill), mock.patch.object(
                self.mod.os, "getpid", return_value=999
            ):
                killed = self.mod.cleanup_stale_sidecars("this-window")

            self.assertEqual(killed, [111])
            self.assertEqual(killed_pids, [111])
            self.assertFalse(mine.exists())

    def test_migrate_legacy_sidecar_pid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "sessions").mkdir()
            legacy = root / "sidecar.pid"
            legacy.write_text("222\n", encoding="utf-8")
            killed_pids: list[int] = []

            with mock.patch.object(self.mod, "_mpftp_dir", return_value=root), mock.patch.object(
                self.mod, "_pid_alive", return_value=True
            ), mock.patch.object(
                self.mod, "_force_kill_pid", side_effect=lambda p: killed_pids.append(p)
            ), mock.patch.object(self.mod.os, "getpid", return_value=1):
                killed = self.mod.migrate_legacy_sidecar_pid()

            self.assertEqual(killed, [222])
            self.assertEqual(killed_pids, [222])
            self.assertFalse(legacy.exists())

    def test_reap_dead_session_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sessions = root / "sessions"
            sessions.mkdir()
            dead = sessions / "gone.pid"
            live = sessions / "live.pid"
            dead.write_text("1\n", encoding="utf-8")
            live.write_text("2\n", encoding="utf-8")

            def fake_alive(pid: int) -> bool:
                return pid == 2

            with mock.patch.object(self.mod, "_mpftp_dir", return_value=root), mock.patch.object(
                self.mod, "_pid_alive", side_effect=fake_alive
            ):
                removed = self.mod.reap_dead_session_pid_files()

            self.assertIn("gone.pid", removed)
            self.assertFalse(dead.exists())
            self.assertTrue(live.exists())

    def test_claim_and_release(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "sessions").mkdir()
            with mock.patch.object(self.mod, "_mpftp_dir", return_value=root):
                path = self.mod.claim_sidecar_pid("claim-test")
                self.assertTrue(path.exists())
                self.assertEqual(int(path.read_text().strip()), os.getpid())
                self.mod.release_sidecar_pid("claim-test")
                self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
