"""Structured {"ok": false, "error", "hint"} failures instead of bare text
or a confusing "Expecting value" JSONDecodeError (mpftp#15)."""

from __future__ import annotations

import io
import socket
import threading
import unittest
from contextlib import redirect_stdout
from unittest import mock

from mpftp.cli import TcpClient, _emit_error_envelope, _hint_for


class HintForTests(unittest.TestCase):
    def test_bad_json_response_gets_a_recovery_hint(self):
        hint = _hint_for("Expecting value: line 1 column 1 (char 0)")
        self.assertIn("mpftp status", hint)

    def test_dead_rpc_session_message_gets_a_recovery_hint(self):
        hint = _hint_for("no response from mpftp RPC session at 127.0.0.1:9 (connection closed...)")
        self.assertIn("mpftp status", hint)

    def test_missing_file_gets_a_path_hint(self):
        hint = _hint_for("[Errno 2] No such file or directory: '/result.txt'")
        self.assertIn("path not found", hint)

    def test_already_actionable_error_gets_no_redundant_hint(self):
        # sidecar's own error text already ends with a checklist; adding a
        # second, generic hint on top would just be noise.
        msg = "could not take control of the board: Access is denied. Your options: ..."
        self.assertIsNone(_hint_for(msg))


class EmitErrorEnvelopeTests(unittest.TestCase):
    def test_prints_ok_false_with_error_and_hint(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            _emit_error_envelope(RuntimeError("Expecting value: line 1 column 1 (char 0)"))
        import json

        envelope = json.loads(buf.getvalue())
        self.assertEqual(envelope["ok"], False)
        self.assertIn("Expecting value", envelope["error"])
        self.assertIn("mpftp status", envelope["hint"])

    def test_omits_hint_key_when_none_applies(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            _emit_error_envelope(RuntimeError("hash mismatch: expected aa, got bb"))
        import json

        envelope = json.loads(buf.getvalue())
        self.assertEqual(envelope, {"ok": False, "error": "hash mismatch: expected aa, got bb"})


class TcpClientEmptyResponseTests(unittest.TestCase):
    """A dead RPC session closing the socket with no reply used to surface as
    a bare json.JSONDecodeError ("Expecting value..."), not a story an agent
    (or a human) could act on."""

    def test_connection_closed_without_a_reply_is_a_clear_error(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        host, port = server.getsockname()

        def accept_and_close():
            conn, _ = server.accept()
            conn.recv(65536)  # drain the request
            conn.close()  # ...then hang up with no reply

        t = threading.Thread(target=accept_and_close, daemon=True)
        t.start()
        try:
            client = TcpClient(host, port)
            with self.assertRaises(RuntimeError) as ctx:
                client.call("ping")
            self.assertIn("no response", str(ctx.exception))
            self.assertNotIn("Expecting value", str(ctx.exception))
        finally:
            server.close()
            t.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
