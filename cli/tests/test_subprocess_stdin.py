"""Every child process the sidecar starts must not inherit its stdin (no board required).

The sidecar reads JSON requests from stdin on a reader thread. On Windows a
child that inherits that pipe blocks during startup while the read is pending,
so ``mpftp circup`` hung for good with circup never reaching its first line.
"""

from __future__ import annotations

import ast
import importlib.util
import unittest
from pathlib import Path

_CALLS = {"run", "check_output", "check_call", "call", "Popen"}


def _subprocess_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _CALLS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
        ):
            yield node


class SidecarStdinTests(unittest.TestCase):
    def test_sidecar_children_do_not_inherit_stdin(self):
        # Read the source rather than importing it: importing the sidecar
        # rewires stdio, which the test runner owns.
        path = Path(importlib.util.find_spec("mpftp.sidecar").origin)
        calls = list(_subprocess_calls(ast.parse(path.read_text(encoding="utf-8"))))
        self.assertTrue(calls, "found no subprocess calls; the scan is broken")
        missing = [c.lineno for c in calls if not any(k.arg == "stdin" for k in c.keywords)]
        self.assertEqual(missing, [], f"sidecar.py subprocess calls without stdin= at lines {missing}")


if __name__ == "__main__":
    unittest.main()
