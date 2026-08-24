"""mpftp local PWA backend: WS codec, static path safety, sidecar relay (mpftp#11 phase 11).

No board, browser, or built UI required.
"""

from __future__ import annotations

import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


def _load_pwa():
    from mpftp import pwa

    return pwa


class WsAcceptKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_pwa()

    def test_matches_the_rfc6455_worked_example(self):
        # https://datatracker.ietf.org/doc/html/rfc6455#section-1.3
        self.assertEqual(
            self.mod._ws_accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )


class ContentTypeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_pwa()

    def test_known_suffixes(self):
        self.assertEqual(self.mod._content_type(".html"), "text/html; charset=utf-8")
        self.assertEqual(self.mod._content_type(".JS"), "text/javascript; charset=utf-8")

    def test_unknown_suffix_falls_back_to_octet_stream(self):
        self.assertEqual(self.mod._content_type(".xyz"), "application/octet-stream")


class ResolveStaticPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_pwa()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "index.html").write_text("<html></html>")
        (self.root / "assets").mkdir()
        (self.root / "assets" / "app.js").write_text("console.log(1)")
        outside = self.root.parent / "secret.txt"
        outside.write_text("nope")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(lambda: outside.unlink(missing_ok=True))

    def test_root_path_serves_index_html(self):
        target = self.mod._resolve_static_path(self.root, "/")
        self.assertEqual(target, self.root / "index.html")

    def test_nested_asset_resolves(self):
        target = self.mod._resolve_static_path(self.root, "/assets/app.js")
        self.assertEqual(target, (self.root / "assets" / "app.js").resolve())

    def test_query_string_is_ignored(self):
        target = self.mod._resolve_static_path(self.root, "/index.html?v=2")
        self.assertEqual(target, self.root / "index.html")

    def test_missing_file_is_none(self):
        self.assertIsNone(self.mod._resolve_static_path(self.root, "/nope.js"))

    def test_path_traversal_outside_root_is_refused(self):
        self.assertIsNone(self.mod._resolve_static_path(self.root, "/../secret.txt"))


class WebSocketFrameCodecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_pwa()

    def _connected_pair(self):
        server_sock, client_sock = socket.socketpair()
        self.addCleanup(server_sock.close)
        return self.mod.WebSocket(server_sock), client_sock

    def _mask(self, payload: bytes) -> bytes:
        key = b"\x01\x02\x03\x04"
        masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        return key + masked

    def test_send_text_is_readable_as_a_plain_ws_frame(self):
        ws, client_sock = self._connected_pair()
        try:
            ws.send_text("hello")
            frame = client_sock.recv(4096)
            # FIN(1) + opcode 0x1 (text), unmasked, short length
            self.assertEqual(frame[0], 0x81)
            self.assertEqual(frame[1], len(b"hello"))
            self.assertEqual(frame[2:], b"hello")
        finally:
            client_sock.close()

    def test_recv_text_unmasks_a_client_frame(self):
        ws, client_sock = self._connected_pair()
        try:
            payload = b'{"method":"ping"}'
            frame = bytes([0x81, 0x80 | len(payload)]) + self._mask(payload)
            client_sock.sendall(frame)
            text = ws.recv_text()
            self.assertEqual(text, payload.decode())
        finally:
            client_sock.close()

    def test_a_ping_gets_an_automatic_pong_and_reading_continues(self):
        ws, client_sock = self._connected_pair()
        try:
            ping = bytes([0x89, 0x80]) + self._mask(b"")
            payload = b"after-ping"
            text_frame = bytes([0x81, 0x80 | len(payload)]) + self._mask(payload)
            client_sock.sendall(ping + text_frame)
            text = ws.recv_text()
            pong = client_sock.recv(16)
            self.assertEqual(pong[0], 0x8A)  # FIN + pong opcode
            self.assertEqual(text, "after-ping")
        finally:
            client_sock.close()

    def test_a_close_frame_ends_recv_text_with_none(self):
        ws, client_sock = self._connected_pair()
        try:
            close_frame = bytes([0x88, 0x80]) + self._mask(b"")
            client_sock.sendall(close_frame)
            self.assertIsNone(ws.recv_text())
        finally:
            client_sock.close()

    def test_socket_eof_ends_recv_text_with_none(self):
        ws, client_sock = self._connected_pair()
        client_sock.close()
        self.assertIsNone(ws.recv_text())

    def test_a_long_payload_uses_the_16_bit_length_prefix(self):
        ws, client_sock = self._connected_pair()
        try:
            ws.send_text("x" * 1000)
            header = client_sock.recv(4)
            self.assertEqual(header[1], 126)
            length = int.from_bytes(header[2:4], "big")
            self.assertEqual(length, 1000)
        finally:
            client_sock.close()


class SidecarRelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_pwa()

    def _relay_with_fake_proc(self, lines):
        proc = mock.Mock()
        proc.stdin = mock.Mock()
        proc.stdout = [*lines, ""]
        with mock.patch.object(self.mod.subprocess, "Popen", return_value=proc):
            relay = self.mod.SidecarRelay("python3")
        relay._reader.join(timeout=2)
        return relay, proc

    def test_broadcasts_every_sidecar_line_to_subscribed_sockets(self):
        relay, proc = self._relay_with_fake_proc(
            ['{"type":"notify","method":"repl_data"}\n', '{"type":"result","id":1}\n']
        )
        ws = mock.Mock()
        relay.subscribe(ws)
        # The reader thread already drained proc.stdout by the time it joined above,
        # so re-broadcast is exercised directly for a deterministic assertion.
        relay._broadcast('{"type":"notify","method":"repl_data"}')
        ws.send_text.assert_called_with('{"type":"notify","method":"repl_data"}')

    def test_send_writes_a_newline_terminated_line_to_stdin(self):
        relay, proc = self._relay_with_fake_proc([])
        relay.send('{"method":"ping"}')
        proc.stdin.write.assert_called_once_with('{"method":"ping"}\n')
        proc.stdin.flush.assert_called_once()

    def test_a_dead_subscriber_is_dropped_after_a_failed_send(self):
        relay, proc = self._relay_with_fake_proc([])
        ws = mock.Mock()
        ws.send_text.side_effect = OSError("broken pipe")
        relay.subscribe(ws)
        relay._broadcast("line")
        self.assertNotIn(ws, relay._subscribers)

    def test_unsubscribe_stops_further_broadcasts(self):
        relay, proc = self._relay_with_fake_proc([])
        ws = mock.Mock()
        relay.subscribe(ws)
        relay.unsubscribe(ws)
        relay._broadcast("line")
        ws.send_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
