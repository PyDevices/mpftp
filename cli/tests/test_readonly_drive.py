"""An upload to a CIRCUITPY drive the computer can't write (mpftp#158)."""

from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class ReadOnlyDriveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from mpftp import sidecar

        cls.mod = sidecar

    def _session(self, root):
        s = self.mod.Session()
        s.interpreter = "circuitpython"
        s._circuitpy_msc_root = lambda: Path(root)
        return s

    def test_a_refused_write_says_the_drive_is_read_only_to_the_computer(self):
        refused = PermissionError(13, "Permission denied", "D:\\t20k.bin")
        with tempfile.TemporaryDirectory() as tmp:
            s = self._session(tmp)
            with (
                mock.patch.object(Path, "write_bytes", side_effect=refused),
                self.assertRaises(RuntimeError) as ctx,
            ):
                s.fs_write("/t20k.bin", base64.b64encode(b"x").decode())
        text = str(ctx.exception)
        self.assertIn("[Errno 13] Permission denied", text)  # what the CLI says
        self.assertIn("read-only to the computer", text)

    def test_a_writable_drive_still_takes_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self._session(tmp)
            s.fs_write("/a.txt", base64.b64encode(b"hi").decode())
            self.assertEqual((Path(tmp) / "a.txt").read_bytes(), b"hi")

    def test_a_usb_upload_batch_leaves_the_boards_repl_alone(self):
        # transfer_begin/transfer_end only matter for Wi-Fi power save. Over
        # USB they used to enter raw REPL twice per upload, interrupting the
        # board's program and making the REPL pane reprint its banner.
        for interp in ("circuitpython", "micropython"):
            s = self.mod.Session()
            s.interpreter = interp
            s.device = s.last_device = "COM7"
            s.transport = mock.Mock(in_raw_repl=False)
            with mock.patch.object(s, "with_raw", side_effect=AssertionError("raw REPL entered")):
                s.transfer_begin()
                s.transfer_end()
            s.transport.enter_raw_repl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
