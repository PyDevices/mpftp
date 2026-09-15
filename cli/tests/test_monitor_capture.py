"""``mpftp monitor``: a bounded read-only console capture.

The capture itself is covered by watch-repl's shape; what is easy to get
wrong is the end of it. The tee runs inside the session, not inside the
socket that asked for it, so a client that just walks away leaves the COM
port held open and the log growing. Both transports must stop it.
"""

from __future__ import annotations

import json
import socket
import threading
import unittest

from mpftp.cli import TcpClient


class TcpClientStopsTheTeeTests(unittest.TestCase):
    def test_a_bounded_capture_sends_debug_tee_stop(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(2)
        host, port = server.getsockname()
        methods: list[str] = []

        def serve():
            # First connection: the capture. Second: whatever the client
            # sends once the duration is up.
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
                    methods.append(req["method"])
                    conn.sendall(
                        (
                            json.dumps(
                                {"type": "result", "id": req["id"], "result": {"ok": True}}
                            )
                            + "\n"
                        ).encode()
                    )
                    if req["method"] == "debug_tee_start":
                        conn.sendall(
                            (
                                json.dumps(
                                    {
                                        "type": "notify",
                                        "method": "debug_tee_data",
                                        "params": {"data_b64": "aGk="},
                                    }
                                )
                                + "\n"
                            ).encode()
                        )
                        # Then go quiet: the client's duration must expire.
                        conn.settimeout(3)
                        try:
                            conn.recv(4096)
                        except (socket.timeout, OSError):
                            pass

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        try:
            client = TcpClient(host, port)
            events: list[tuple[str, dict]] = []
            client.stream_debug_tee(
                "COM4",
                115200,
                None,
                lambda method, params: events.append((method, params)),
                duration=0.25,
            )
        finally:
            t.join(timeout=5)
            server.close()

        self.assertEqual([("debug_tee_data", {"data_b64": "aGk="})], events)
        self.assertEqual(["debug_tee_start", "debug_tee_stop"], methods)


if __name__ == "__main__":
    unittest.main()
