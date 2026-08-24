"""mpftp clean --dry-run: board debris cleanup (mpftp#17). No board required."""

from __future__ import annotations

import argparse
import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from mpftp.cli import DEFAULT_CLEAN_PATTERNS, RpcClient, _find_debris, cmd_clean

TREE = {
    "path": "/",
    "children": [
        {"name": "main.py", "path": "/main.py", "isDir": False, "size": 10},
        {"name": "main.py.bak", "path": "/main.py.bak", "isDir": False, "size": 10},
        {"name": "result.txt", "path": "/result.txt", "isDir": False, "size": 4},
        {"name": "probe_1.py", "path": "/probe_1.py", "isDir": False, "size": 20},
        {
            "name": "__pycache__",
            "path": "/__pycache__",
            "isDir": True,
            "size": 0,
            "children": [
                {"name": "main.mpy", "path": "/__pycache__/main.mpy", "isDir": False, "size": 5}
            ],
        },
        {
            "name": "lib",
            "path": "/lib",
            "isDir": True,
            "size": 0,
            "children": [
                {"name": "adafruit_bus_device", "path": "/lib/adafruit_bus_device", "isDir": True, "size": 0},
                {"name": "helper.bak", "path": "/lib/helper.bak", "isDir": False, "size": 3},
            ],
        },
    ],
}


class FindDebrisTests(unittest.TestCase):
    def test_matches_default_patterns_across_the_whole_tree(self):
        matched = []
        _find_debris(TREE, list(DEFAULT_CLEAN_PATTERNS), matched)
        paths = sorted(m["path"] for m in matched)
        self.assertEqual(
            paths,
            ["/__pycache__", "/lib/helper.bak", "/main.py.bak", "/probe_1.py", "/result.txt"],
        )

    def test_does_not_descend_into_a_matched_directory(self):
        matched = []
        _find_debris(TREE, ["__pycache__"], matched)
        self.assertEqual([m["path"] for m in matched], ["/__pycache__"])
        # The file inside it must not appear separately -- fs_rm_rf covers it.
        self.assertNotIn("/__pycache__/main.mpy", [m["path"] for m in matched])

    def test_custom_pattern_replaces_rather_than_extends_defaults(self):
        matched = []
        _find_debris(TREE, ["main.py"], matched)
        self.assertEqual([m["path"] for m in matched], ["/main.py"])


class FakeClient(RpcClient):
    def __init__(self, tree):
        self.calls: list[tuple[str, dict]] = []
        self.tree = tree

    def call(self, method, params=None):
        self.calls.append((method, params or {}))
        if method == "fs_tree":
            return self.tree
        return {"ok": True}


class CmdCleanTests(unittest.TestCase):
    def _ns(self, **overrides):
        base = dict(device=None, baud=115200, path="/", dry_run=False, pattern=None)
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_dry_run_lists_matches_without_deleting_anything(self):
        client = FakeClient(TREE)
        with mock.patch("mpftp.cli.get_client", return_value=(client, "tcp")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cmd_clean(self._ns(dry_run=True))
        import json

        result = json.loads(buf.getvalue())
        self.assertTrue(result["dry_run"])
        self.assertEqual(len(result["matched"]), 5)
        methods = [c[0] for c in client.calls]
        self.assertNotIn("fs_rm", methods)
        self.assertNotIn("fs_rm_rf", methods)

    def test_real_run_removes_files_and_rf_removes_directories(self):
        client = FakeClient(TREE)
        with mock.patch("mpftp.cli.get_client", return_value=(client, "tcp")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cmd_clean(self._ns(dry_run=False))
        import json

        result = json.loads(buf.getvalue())
        self.assertFalse(result["dry_run"])
        self.assertEqual(
            sorted(result["removed"]),
            ["/__pycache__", "/lib/helper.bak", "/main.py.bak", "/probe_1.py", "/result.txt"],
        )
        rm_call = next(c for c in client.calls if c[1].get("path") == "/main.py.bak")
        self.assertEqual(rm_call[0], "fs_rm")
        rf_call = next(c for c in client.calls if c[1].get("path") == "/__pycache__")
        self.assertEqual(rf_call[0], "fs_rm_rf")

    def test_a_failed_removal_is_reported_not_raised(self):
        client = FakeClient(TREE)

        def call(method, params=None):
            client.calls.append((method, params or {}))
            if method == "fs_tree":
                return TREE
            if method == "fs_rm" and params.get("path") == "/main.py.bak":
                raise RuntimeError("access denied")
            return {"ok": True}

        client.call = call
        with mock.patch("mpftp.cli.get_client", return_value=(client, "tcp")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cmd_clean(self._ns(dry_run=False))
        import json

        result = json.loads(buf.getvalue())
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["path"], "/main.py.bak")
        self.assertNotIn("/main.py.bak", result["removed"])

    def test_custom_patterns_are_forwarded(self):
        client = FakeClient(TREE)
        with mock.patch("mpftp.cli.get_client", return_value=(client, "tcp")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cmd_clean(self._ns(dry_run=True, pattern=["*.py"]))
        import json

        result = json.loads(buf.getvalue())
        self.assertEqual(result["patterns"], ["*.py"])


if __name__ == "__main__":
    unittest.main()
