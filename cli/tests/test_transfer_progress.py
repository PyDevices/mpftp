"""Bytes moving inside one large file count as activity (mpftp#149).

A 2.8 MB download over USB serial is a single request for about twelve
minutes. The panel's monitor used to hear nothing until the file finished
and called the transfer hung. The sidecar now sends ``transfer_progress``
notes while a file moves, and the monitor treats each as activity.
"""

from __future__ import annotations

import base64
import json
import tempfile
import threading
import time
import unittest
from unittest import mock

from mpftp import panel, pwa, sidecar


class MonitorTests(unittest.TestCase):
    def _monitor(self):
        updates = []
        m = panel.TransferMonitor(
            "Downloading", 1, lambda text, phase: updates.append(phase), stall=0.3, tick=0.05
        )
        m.start()
        m.begin_file("/big.bin")
        return m, updates

    def test_progress_notes_keep_a_slow_file_active(self):
        m, updates = self._monitor()
        for _ in range(12):  # 0.6 s, twice the stall window
            time.sleep(0.05)
            m.note_progress()
        m.dispose()
        self.assertNotIn("stalled", updates)

    def test_no_bytes_for_the_stall_window_is_still_reported_hung(self):
        m, updates = self._monitor()
        time.sleep(0.6)
        m.dispose()
        self.assertIn("stalled", updates)


class SidecarProgressTests(unittest.TestCase):
    def test_fs_read_reports_bytes_while_the_file_moves(self):
        class T:
            def fs_readfile(self, path, progress_callback=None):
                for n in (10, 20, 30):
                    progress_callback(n, 30)
                    time.sleep(0.02)
                return bytearray(30)

        s = sidecar.Session()
        s.with_raw = lambda fn: fn(T())
        s._transfer = lambda fn: fn
        seen = []
        with mock.patch.object(
            sidecar, "_notify", lambda m, p: seen.append((m, p))
        ), mock.patch.object(sidecar.time, "monotonic", side_effect=iter(range(10, 100, 2))):
            s.fs_read("/big.bin")
        self.assertEqual(
            [p["done"] for m, p in seen if m == "transfer_progress"], [10, 20, 30]
        )


class RelayRoutingTests(unittest.TestCase):
    def test_a_slow_download_that_keeps_moving_is_never_flagged(self):
        proc = mock.Mock()
        proc.stdout = [""]
        with mock.patch.object(pwa.subprocess, "Popen", return_value=proc):
            relay = pwa.SidecarRelay("python3", local_path=tempfile.gettempdir())
        relay._reader.join(timeout=2)
        phases = []
        data = b"x" * 10

        def request(method, params=None):
            if method == "fs_stat":
                return {"isDir": False, "size": 10}
            if method == "fs_read":
                for _ in range(12):  # 0.6 s of motion, twice the stall window
                    time.sleep(0.05)
                    relay._route(json.dumps({"type": "notify", "method": "transfer_progress",
                                             "params": {"path": "/big.bin", "done": 1, "total": 10}}))
                return {"data_b64": base64.b64encode(data).decode()}
            if method == "fs_hash":
                import hashlib
                return {"hash": hashlib.sha256(data).hexdigest()}
            return {}

        host = relay.panel
        host.request = request
        host.connected_device = "COM1"
        host.status = lambda text, phase="idle": phases.append(phase)
        real = panel.TransferMonitor
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            panel, "TransferMonitor",
            lambda v, t, cb: real(v, t, cb, stall=0.3, tick=0.05),
        ):
            host.local_path = td
            host.download_many(object(), ["/big.bin"])
        self.assertNotIn("stalled", phases)
        self.assertIn("active", phases)


if __name__ == "__main__":
    unittest.main()
