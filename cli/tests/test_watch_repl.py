"""Non-interrupting live tail of the board's own stdout (mpftp#10).

Both transports stream repl_data/repl_error notify events to a callback
without ever sending anything that would enter raw REPL. No board required.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import unittest
from unittest import mock

from mpftp.cli import SidecarClient, TcpClient


class TcpClientStreamReplTests(unittest.TestCase):
    def test_streams_repl_data_and_ignores_other_notify_methods(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        host, port = server.getsockname()

        def serve():
            conn, _ = server.accept()
            with conn:
                buf = b""
                while b"\n" not in buf:
                    buf += conn.recv(4096)
                req = json.loads(buf.split(b"\n", 1)[0])
                self.assertEqual(req["method"], "repl_stream")
                conn.sendall(
                    (json.dumps({"type": "result", "id": req["id"], "result": {"ok": True}}) + "\n").encode()
                )
                conn.sendall(
                    (
                        json.dumps(
                            {"type": "notify", "method": "repl_data", "params": {"data_b64": "aGk="}}
                        )
                        + "\n"
                    ).encode()
                )
                # A notify type the caller doesn't care about should be skipped, not crash.
                conn.sendall((json.dumps({"type": "notify", "method": "mip_progress", "params": {}}) + "\n").encode())
                conn.sendall(
                    (
                        json.dumps(
                            {"type": "notify", "method": "repl_error", "params": {"message": "boom"}}
                        )
                        + "\n"
                    ).encode()
                )
                # Closing ends the client's read loop cleanly (no error).

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        try:
            client = TcpClient(host, port)
            events = []
            client.stream_repl(lambda method, params: events.append((method, params)))
        finally:
            server.close()
            t.join(timeout=2)

        self.assertEqual(
            events,
            [("repl_data", {"data_b64": "aGk="}), ("repl_error", {"message": "boom"})],
        )

    def test_raises_on_an_error_reply(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        host, port = server.getsockname()

        def serve():
            conn, _ = server.accept()
            with conn:
                buf = b""
                while b"\n" not in buf:
                    buf += conn.recv(4096)
                req = json.loads(buf.split(b"\n", 1)[0])
                conn.sendall(
                    (json.dumps({"type": "error", "id": req["id"], "error": "not connected"}) + "\n").encode()
                )

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        try:
            client = TcpClient(host, port)
            with self.assertRaises(RuntimeError) as ctx:
                client.stream_repl(lambda method, params: None)
            self.assertIn("not connected", str(ctx.exception))
        finally:
            server.close()
            t.join(timeout=2)

    def test_a_duration_returns_even_though_the_connection_stays_open(self):
        """The MCP watch_repl tool needs a bounded call (mpftp#19)."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        host, port = server.getsockname()

        def serve():
            conn, _ = server.accept()
            with conn:
                buf = b""
                while b"\n" not in buf:
                    buf += conn.recv(4096)
                req = json.loads(buf.split(b"\n", 1)[0])
                conn.sendall(
                    (json.dumps({"type": "result", "id": req["id"], "result": {"ok": True}}) + "\n").encode()
                )
                conn.sendall(
                    (
                        json.dumps(
                            {"type": "notify", "method": "repl_data", "params": {"data_b64": "aGk="}}
                        )
                        + "\n"
                    ).encode()
                )
                time.sleep(2)  # outlives the client's duration; never sends more or closes

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        try:
            client = TcpClient(host, port)
            events = []
            started = time.monotonic()
            client.stream_repl(lambda method, params: events.append((method, params)), duration=0.3)
            elapsed = time.monotonic() - started
        finally:
            server.close()
            t.join(timeout=3)

        self.assertLess(elapsed, 2.0)
        self.assertEqual(events, [("repl_data", {"data_b64": "aGk="})])


class SidecarClientStreamReplTests(unittest.TestCase):
    def _client_with_fake_proc(self, lines: list[str]):
        client = SidecarClient.__new__(SidecarClient)
        client._id = 0
        client.proc = mock.Mock()
        client.proc.stdin = mock.Mock()
        client.proc.stdout = mock.Mock()
        client.proc.stdout.readline.side_effect = [*lines, ""]
        client.proc.stderr = mock.Mock()
        client.proc.stderr.read.return_value = ""
        return client

    def test_sends_repl_start_and_streams_notify_events(self):
        lines = [
            json.dumps({"type": "result", "id": 1, "result": {"device": "COM4"}}) + "\n",
            json.dumps({"type": "notify", "method": "repl_data", "params": {"data_b64": "aGk="}}) + "\n",
        ]
        client = self._client_with_fake_proc(lines)
        events = []
        # readline() eventually returns "" (fake stdout EOF) once the scripted
        # lines run out — stream_repl correctly treats that as the sidecar
        # having exited and raises, same as a real process closing stdout.
        with self.assertRaises(RuntimeError):
            client.stream_repl(lambda method, params: events.append((method, params)))

        sent = json.loads(client.proc.stdin.write.call_args[0][0])
        self.assertEqual(sent["method"], "repl_start")
        self.assertEqual(events, [("repl_data", {"data_b64": "aGk="})])

    def test_raises_on_an_error_reply_for_this_request_id(self):
        lines = [json.dumps({"type": "error", "id": 1, "error": "not connected"}) + "\n"]
        client = self._client_with_fake_proc(lines)
        with self.assertRaises(RuntimeError) as ctx:
            client.stream_repl(lambda method, params: None)
        self.assertIn("not connected", str(ctx.exception))

    def test_a_duration_returns_even_though_readline_never_unblocks(self):
        """The reader thread outlives the call — fine for a one-shot bounded capture."""
        client = SidecarClient.__new__(SidecarClient)
        client._id = 0
        client.proc = mock.Mock()
        client.proc.stdin = mock.Mock()
        client.proc.stdout = mock.Mock()
        client.proc.stdout.readline.side_effect = lambda: threading.Event().wait() or ""

        started = time.monotonic()
        client.stream_repl(lambda method, params: None, duration=0.2)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0)


if __name__ == "__main__":
    unittest.main()
