"""Unit tests that do not require a board."""

from __future__ import annotations

import unittest
from pathlib import Path


def _load_sidecar():
    """Import mpftp.sidecar, insisting it is the one in ``cli/src``.

    Run without ``PYTHONPATH=cli/src`` (the invocation in AGENTS.md), this
    resolves to whatever copy is installed in ``.venv`` instead -- and the venv
    holds a real copy, not an editable link. The suite then reports on code
    that is not in the working tree: an edit you just made looks like a
    failure, and a regression you just introduced looks like a pass. Fail
    loudly with the fix rather than either.
    """
    from mpftp import sidecar

    want = Path(__file__).resolve().parents[1] / "src" / "mpftp" / "sidecar.py"
    got = Path(sidecar.__file__).resolve()
    if got != want:
        raise AssertionError(
            f"imported {got}, not the working tree's {want}.\n"
            "Run the suite as AGENTS.md documents:\n"
            "  PYTHONPATH=cli/src python3 -m unittest discover -s cli/tests"
        )
    return sidecar


class HelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_sidecar()

    def test_split_fs_path(self):
        self.assertEqual(self.mod.split_fs_path(":/main.py"), (True, "/main.py"))
        self.assertEqual(self.mod.split_fs_path("./main.py"), (False, "./main.py"))
        self.assertEqual(self.mod.split_fs_path("/tmp/a"), (False, "/tmp/a"))

    def test_host_sha256(self):
        self.assertEqual(
            self.mod.host_sha256(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )

    def test_should_skip_transfer_entry(self):
        skip = self.mod.should_skip_transfer_entry
        self.assertTrue(skip(".git"))
        self.assertTrue(skip(".venv"))
        self.assertTrue(skip(".env"))
        self.assertTrue(skip("__pycache__"))
        self.assertTrue(skip("foo.pyc"))
        self.assertTrue(skip("bar.PYO"))
        self.assertFalse(skip("main.py"))
        self.assertFalse(skip("lib"))
        self.assertFalse(skip("pycache"))

    def test_host_rtc_tuple_shape(self):
        tup = self.mod.Session()._host_rtc_tuple()
        self.assertEqual(len(tup), 8)
        year, month, day, wday, hour, minute, sec, sub = tup
        self.assertGreaterEqual(year, 2024)
        self.assertTrue(1 <= month <= 12)
        self.assertTrue(1 <= day <= 31)
        self.assertTrue(0 <= wday <= 6)
        self.assertTrue(0 <= hour <= 23)
        self.assertEqual(sub, 0)

    def test_dead_serial_and_eof_helpers(self):
        self.assertTrue(
            self.mod.is_dead_serial_error(PermissionError(13, "Access is denied."))
        )
        self.assertTrue(
            self.mod.is_eof_timeout_error(
                RuntimeError("timeout waiting for first EOF reception")
            )
        )
        self.assertFalse(
            self.mod.is_dead_serial_error(RuntimeError("could not enter raw repl"))
        )
        msg = self.mod.friendly_exec_timeout_message(
            "timeout waiting for first EOF reception"
        )
        self.assertIn("--no-follow", msg)
        # The hint used to offer only ways to STOP the program (interrupt,
        # soft-reset, hard-reset), so the obvious next move on a board with one
        # USB port was to reconnect -- which takes the raw REPL and kills the
        # very program you were trying to observe. Name the read-only command.
        self.assertIn("monitor", msg)

    def test_annotate_port_roles_dual_usb(self):
        ports = [
            {"device": "COM49", "vid": 0x1A86, "pid": 1, "repl": True},
            {"device": "COM50", "vid": 0x303A, "pid": 1, "repl": True},
            {"device": "COM51", "vid": 0x303A, "pid": 2, "repl": False},
        ]
        self.mod.annotate_port_roles(ports)
        self.assertEqual(ports[0]["role"], "repl")
        self.assertEqual(ports[1]["role"], "cdc_debug")
        self.assertEqual(ports[2]["role"], "data")


if __name__ == "__main__":
    unittest.main()
