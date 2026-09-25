"""``mpftp hard-reset --monitor SECONDS`` (mpftp#60).

Reset, let main.py run, then capture the boot read-only on the same port. The
hard parts are a native-USB port that disappears and comes back while the
capture is open, a capture clock that must not run out while it's gone, and a
TinyUSB CDC port that sends nothing unless DTR is up. No board needed.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import socket
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from test_helpers import _load_sidecar


class FakePort:
    """One COM name whose device can vanish and re-enumerate."""

    def __init__(self):
        self.present = True
        self.generation = 0
        self.pending: dict[int, list[bytes]] = {}
        self.opens: list[tuple[bool, bool]] = []  # (dtr, rts) at each open
        self.lock = threading.Lock()

    def reset(self):
        """The board resets: the node goes away."""
        with self.lock:
            self.present = False
            self.generation += 1

    def come_back(self, data: bytes):
        with self.lock:
            self.pending.setdefault(self.generation, []).append(data)
            self.present = True


class FakeSerial:
    def __init__(self, port: FakePort):
        self._p = port
        self.port = None
        self.baudrate = None
        self.timeout = None
        self.dtr = True  # pyserial's default; the tee must lower it
        self.rts = True
        self.gen = None

    def open(self):
        with self._p.lock:
            if not self._p.present:
                raise OSError("could not open port: FileNotFoundError")
            self.gen = self._p.generation
            self._p.opens.append((self.dtr, self.rts))

    def _check(self):
        with self._p.lock:
            if self.gen != self._p.generation:
                raise OSError("ClearCommError failed (the device is gone)")

    @property
    def in_waiting(self):
        self._check()
        return sum(len(b) for b in self._p.pending.get(self.gen, []))

    def read(self, n=1):
        self._check()
        with self._p.lock:
            chunks = self._p.pending.get(self.gen, [])
            if chunks:
                return chunks.pop(0)
        time.sleep(0.01)
        return b""

    def close(self):
        pass


class TeeAcrossReEnumerationTests(unittest.TestCase):
    """The sidecar's side: the tee follows the port through a reset."""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_sidecar()

    def _run(self, *, wait, dtr=False):
        port = FakePort()
        session = self.mod.Session()
        session._tee_serial_factory = lambda: FakeSerial(port)
        events: list[tuple[str, dict]] = []
        tmp = self._tmpdir()
        with mock.patch.object(
            self.mod, "_notify", lambda m, p=None: events.append((m, p or {}))
        ):
            result = session.debug_tee_start(
                "COM25", 115200, f"{tmp}/boot.log", wait=wait, dtr=dtr
            )
            # The reset lands after the tee opened the dying node.
            port.reset()
            time.sleep(0.4)
            port.come_back(b"boot: main.py started\r\n")
            deadline = time.time() + 3
            while time.time() < deadline and not any(
                m in ("debug_tee_data", "debug_tee_error") for m, _ in events
            ):
                time.sleep(0.02)
            session.debug_tee_stop()
        return result, events, port

    def _tmpdir(self):
        import tempfile

        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return d.name

    def test_waiting_tee_reopens_the_port_and_captures_the_boot(self):
        result, events, port = self._run(wait=3.0)
        methods = [m for m, _ in events]
        self.assertEqual(
            ["debug_tee_open", "debug_tee_lost", "debug_tee_open", "debug_tee_data"],
            methods,
        )
        data = base64.b64decode(events[-1][1]["data_b64"])
        self.assertEqual(b"boot: main.py started\r\n", data)
        self.assertIn("waited_s", result)
        # Read-only: RTS never raised, DTR only when asked.
        self.assertEqual([(False, False), (False, False)], port.opens)

    def test_without_wait_the_tee_stops_when_the_port_goes(self):
        # Control, and plain `monitor` unchanged: no reopen, no boot output.
        _result, events, port = self._run(wait=None)
        self.assertEqual(["debug_tee_error"], [m for m, _ in events])
        self.assertEqual(1, len(port.opens))

    def test_dtr_is_raised_only_when_asked(self):
        _result, _events, port = self._run(wait=3.0, dtr=True)
        self.assertEqual([(True, False), (True, False)], port.opens)

    def test_a_port_that_never_comes_back_is_an_error_after_wait(self):
        port = FakePort()
        port.present = False
        session = self.mod.Session()
        session._tee_serial_factory = lambda: FakeSerial(port)
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "did not come back within 0.5 s"):
            session.debug_tee_start("COM25", 115200, None, wait=0.5)
        self.assertLess(time.monotonic() - started, 2.0)


class ConsoleDtrTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_sidecar()

    def test_which_ports_get_dtr(self):
        want = self.mod.console_wants_dtr
        # Native TinyUSB CDC: the T-Embed on CircuitPython, MicroPython S3/rp2.
        self.assertTrue(want(0x303A, 0x8151, "circuitpython"))
        self.assertTrue(want(0x303A, 0x4001, "micropython"))
        self.assertTrue(want(0x2E8A, 0x0005, "micropython"))
        self.assertTrue(want(0x2886, 0x802F, "circuitpython"))
        # DTR/RTS reset the chip on these: leave them low.
        self.assertFalse(want(0x1A86, 0x55D3, "micropython"))  # CH343, the P4/LCD-7
        self.assertFalse(want(0x1A86, 0x55D3, "circuitpython"))
        self.assertFalse(want(0x303A, 0x1001, "micropython"))  # USB-Serial-JTAG
        self.assertFalse(want(0x10C4, 0xEA60, "circuitpython"))  # CP210x
        self.assertFalse(want(None, None, "circuitpython"))
        self.assertFalse(want(0x2886, 0x802F, "micropython"))


class _ResetTransport:
    in_raw_repl = True

    def __init__(self):
        self.sent: list[bytes] = []

    def exec_raw_no_follow(self, code):
        self.sent.append(code)


class HardResetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_sidecar()

    def _reset(self, interpreter, vid, pid):
        session = self.mod.Session()
        t = _ResetTransport()
        session.transport = t
        session.device = "COM25"
        session.interpreter = interpreter
        with mock.patch.object(
            session, "_take_control_resilient", lambda *a, **k: t
        ), mock.patch.object(
            session,
            "list_ports",
            lambda: [{"device": "COM25", "vid": vid, "pid": pid}],
        ):
            result = session.hard_reset()
        return result, t.sent

    def test_circuitpython_is_reset_with_microcontroller(self):
        # It used to get machine.reset(): ImportError on the board, unread,
        # and "ok" back while the board kept running.
        result, sent = self._reset("circuitpython", 0x303A, 0x8151)
        self.assertEqual(1, len(sent))
        self.assertIn(b"microcontroller.reset()", sent[0])
        self.assertNotIn(b"machine", sent[0])
        self.assertEqual("COM25", result["device"])
        self.assertTrue(result["console_dtr"])
        self.assertTrue(result["runs_main"])

    def test_micropython_on_a_ch343_keeps_machine_reset_and_dtr_low(self):
        result, sent = self._reset("micropython", 0x1A86, 0x55D3)
        self.assertIn(b"machine.reset()", sent[0])
        self.assertFalse(result["console_dtr"])


class CliHardResetMonitorTests(unittest.TestCase):
    def setUp(self):
        from mpftp import cli

        self.cli = cli

    def _run(self, monitor):
        calls: list[tuple] = []
        cli = self.cli

        class FakeClient(cli.RpcClient):
            def call(self, method, params=None):
                calls.append(("call", method))
                return {"ok": True, "device": "COM25", "console_dtr": True}

            def stream_debug_tee(self, device, baud, log_path, on_notify, duration=None, wait=None, dtr=False):
                calls.append(("stream", device, duration, wait, dtr))
                on_notify("debug_tee_data", {"data_b64": base64.b64encode(b"hello\n").decode()})

            def close(self):
                calls.append(("close",))

        ns = argparse.Namespace(device="COM25", baud=115200, monitor=monitor, log_path=None)
        out, err = io.StringIO(), io.StringIO()
        stdout = mock.MagicMock()
        stdout.buffer = io.BytesIO()
        stdout.write = out.write
        with mock.patch.object(cli, "get_client", lambda: (FakeClient(), "sidecar")), \
                mock.patch.object(cli, "ensure_device", lambda *a: None), \
                redirect_stderr(err), redirect_stdout(stdout):
            cli.cmd_hard_reset(ns)
        return calls, out.getvalue(), stdout.buffer.getvalue(), err.getvalue()

    def test_plain_hard_reset_is_unchanged(self):
        calls, out, raw, _err = self._run(None)
        self.assertEqual([("call", "hard_reset"), ("close",)], calls)
        self.assertEqual("COM25", json.loads(out)["device"])
        self.assertEqual(b"", raw)

    def test_monitor_streams_the_same_port_after_the_reset(self):
        calls, _out, raw, err = self._run(7.0)
        self.assertEqual(
            [
                ("call", "hard_reset"),
                ("stream", "COM25", 7.0, self.cli.RESET_MONITOR_WAIT, True),
                ("close",),
            ],
            calls,
        )
        self.assertEqual(b"hello\n", raw)
        self.assertIn('"device": "COM25"', err)


class McpHardResetTests(unittest.TestCase):
    def test_monitor_seconds_returns_the_boot_text(self):
        from mpftp import cli, mcp

        calls: list[tuple] = []

        class FakeClient(cli.RpcClient):
            def call(self, method, params=None):
                calls.append(("call", method))
                return {"ok": True, "device": "COM25", "console_dtr": False}

            def stream_debug_tee(self, device, baud, log_path, on_notify, duration=None, wait=None, dtr=False):
                calls.append(("stream", device, duration, wait, dtr))
                on_notify("debug_tee_data", {"data_b64": base64.b64encode(b"Traceback\n").decode()})

            def close(self):
                pass

        with mock.patch.object(mcp, "get_client", lambda: (FakeClient(), "sidecar")), \
                mock.patch.object(mcp, "ensure_device", lambda *a: None):
            got = mcp._tool_hard_reset({"device": "COM25", "monitor_seconds": 4})
            plain = mcp._tool_hard_reset({"device": "COM25"})
        self.assertEqual("Traceback\n", got["text"])
        self.assertEqual(("stream", "COM25", 4.0, cli.RESET_MONITOR_WAIT, False), calls[1])
        self.assertEqual("COM25", plain["device"])
        self.assertEqual(3, len(calls))


class CaptureClockTests(unittest.TestCase):
    """The capture's SECONDS must not run out while the port is gone."""

    def test_tcp_client_pauses_the_clock_while_the_port_is_gone(self):
        from mpftp.cli import TcpClient

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(2)
        host, port = server.getsockname()

        def send(conn, obj):
            conn.sendall((json.dumps(obj) + "\n").encode())

        def serve():
            for _ in range(2):
                conn, _addr = server.accept()
                with conn:
                    buf = b""
                    while b"\n" not in buf:
                        chunk = conn.recv(4096)
                        if not chunk:
                            return
                        buf += chunk
                    req = json.loads(buf.split(b"\n", 1)[0])
                    if req["method"] != "debug_tee_start":
                        send(conn, {"type": "result", "id": req["id"], "result": {}})
                        continue
                    def note(m, p, conn=conn):
                        send(conn, {"type": "notify", "method": m, "params": p})

                    note("debug_tee_open", {"device": "COM25", "after_s": 0.0})
                    send(conn, {"type": "result", "id": req["id"], "result": {"ok": True}})
                    note("debug_tee_lost", {"device": "COM25"})
                    # Gone for longer than the whole capture.
                    time.sleep(0.6)
                    note("debug_tee_open", {"device": "COM25", "after_s": 0.6})
                    note("debug_tee_data", {"data_b64": "Ym9vdA=="})
                    conn.settimeout(3)
                    try:
                        conn.recv(4096)
                    except (socket.timeout, OSError):
                        pass

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        events: list[str] = []
        try:
            TcpClient(host, port).stream_debug_tee(
                "COM25",
                115200,
                None,
                lambda m, p: events.append(m),
                duration=0.3,
                wait=5.0,
            )
        finally:
            t.join(timeout=5)
            server.close()
        self.assertEqual(
            ["debug_tee_open", "debug_tee_lost", "debug_tee_open", "debug_tee_data"],
            events,
        )

    def test_the_clock_rules(self):
        from mpftp.cli import _tee_reset_clock

        d, done = _tee_reset_clock("debug_tee_open", None, 5.0)
        self.assertAlmostEqual(d, time.time() + 5.0, delta=0.5)
        self.assertFalse(done)
        self.assertEqual((None, False), _tee_reset_clock("debug_tee_lost", 123.0, 5.0))
        self.assertEqual((123.0, True), _tee_reset_clock("debug_tee_error", 123.0, 5.0))
        self.assertEqual((123.0, False), _tee_reset_clock("debug_tee_data", 123.0, 5.0))


if __name__ == "__main__":
    unittest.main()
