"""probe: run -> wait -> capture in one shot (mpftp#11).

Exercises cmd_probe/_wait_and_reconnect against a fake RpcClient (no board,
no sockets) so the sequencing (hard_reset -> reconnect -> run -> capture)
is covered without needing real hardware or a real transport.
"""

from __future__ import annotations

import argparse
import base64
import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from mpftp.cli import RpcClient, _wait_and_reconnect, cmd_probe


class FakeClient(RpcClient):
    def __init__(self, responses=None, port_sequence=None):
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or {}
        # Each call to list_ports pops the next entry (simulates the board
        # taking a couple of polls to reappear after a reset).
        self.port_sequence = list(port_sequence or [])

    def call(self, method, params=None):
        self.calls.append((method, params or {}))
        if method == "list_ports":
            return self.port_sequence.pop(0) if self.port_sequence else []
        if method in self.responses:
            resp = self.responses[method]
            if isinstance(resp, Exception):
                raise resp
            return resp
        return {}


class WaitAndReconnectTests(unittest.TestCase):
    def test_reconnects_once_the_port_reappears(self):
        client = FakeClient(port_sequence=[[], [{"device": "COM4"}]])
        with mock.patch("mpftp.cli.time.sleep"):
            _wait_and_reconnect(client, "COM4", 115200, attempts=5, delay=0)
        self.assertEqual(
            client.calls,
            [
                ("list_ports", {}),
                ("list_ports", {}),
                ("connect", {"device": "COM4", "baud": 115200}),
            ],
        )

    def test_gives_up_after_attempts_are_exhausted(self):
        client = FakeClient(port_sequence=[[]] * 3)
        with mock.patch("mpftp.cli.time.sleep"), self.assertRaises(RuntimeError) as ctx:
            _wait_and_reconnect(client, "COM4", 115200, attempts=3, delay=0)
        self.assertIn("COM4", str(ctx.exception))


class CmdProbeTests(unittest.TestCase):
    def _ns(self, **overrides):
        base = dict(
            device="COM4",
            baud=115200,
            file="",
            reboot_first=False,
            capture=None,
            wait=0.0,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_reboot_first_hard_resets_then_reconnects_before_running(self):
        client = FakeClient(
            responses={"connect": {}, "run_script": {"ok": True}},
            port_sequence=[[{"device": "COM4"}]],
        )
        with mock.patch("mpftp.cli.get_client", return_value=(client, "tcp")), mock.patch(
            "mpftp.cli.time.sleep"
        ), mock.patch("mpftp.cli.Path") as fake_path:
            fake_path.return_value.read_text.return_value = "print('hi')"
            buf = io.StringIO()
            with redirect_stdout(buf):
                cmd_probe(self._ns(reboot_first=True, file="probe.py"))

        methods = [m for m, _ in client.calls]
        # ensure_device's connect, then hard_reset, then the reconnect's own
        # list_ports + connect, then the actual run.
        self.assertIn("hard_reset", methods)
        self.assertEqual(methods.index("hard_reset") < methods.index("run_script"), True)
        self.assertEqual(methods.count("connect"), 2)  # ensure_device + post-reboot reconnect

    def test_reboot_first_without_a_device_is_a_clear_error(self):
        client = FakeClient()
        with mock.patch(
            "mpftp.cli.get_client", return_value=(client, "tcp")
        ), self.assertRaises(RuntimeError) as ctx:
            cmd_probe(self._ns(device=None, reboot_first=True, file="probe.py"))
        self.assertIn("--device", str(ctx.exception))

    def test_captures_the_result_file_as_json(self):
        data = base64.b64encode(b"stage: 3\n").decode("ascii")
        client = FakeClient(
            responses={
                "run_script": {"ok": True},
                "fs_read": {"data_b64": data},
            }
        )
        with mock.patch("mpftp.cli.get_client", return_value=(client, "tcp")), mock.patch(
            "mpftp.cli.time.sleep"
        ), mock.patch("mpftp.cli.Path") as fake_path:
            fake_path.return_value.read_text.return_value = "print('hi')"
            buf = io.StringIO()
            with redirect_stdout(buf):
                cmd_probe(self._ns(capture="/result.txt", wait=1.0, file="probe.py"))

        import json

        printed = json.loads(buf.getvalue())
        self.assertEqual(printed["ok"], True)
        self.assertEqual(printed["capture"]["text"], "stage: 3\n")
        self.assertEqual(printed["capture"]["path"], "/result.txt")

    def test_a_failed_capture_marks_the_result_not_ok_but_still_emits_json(self):
        client = FakeClient(
            responses={
                "run_script": {"ok": True},
                "fs_read": FileNotFoundError("[Errno 2] /result.txt"),
            }
        )
        with mock.patch("mpftp.cli.get_client", return_value=(client, "tcp")), mock.patch(
            "mpftp.cli.time.sleep"
        ), mock.patch("mpftp.cli.Path") as fake_path:
            fake_path.return_value.read_text.return_value = "print('hi')"
            buf = io.StringIO()
            with redirect_stdout(buf):
                cmd_probe(self._ns(capture="/result.txt", file="probe.py"))

        import json

        printed = json.loads(buf.getvalue())
        self.assertEqual(printed["ok"], False)
        self.assertIn("capture_error", printed)


if __name__ == "__main__":
    unittest.main()
