"""``run --follow`` used to discard everything the board printed when the
follow hit its quiet timeout, so a script that printed twenty lines and then
hung looked identical to one that never started (mpftp#25). The partial output
now rides the error through every hop: sidecar exception -> RPC error reply ->
client exception -> the CLI's ``{"ok": false, ...}`` envelope. No board required.
"""

from __future__ import annotations

import io
import json
import socket
import threading
import unittest
from contextlib import redirect_stdout
from unittest import mock

from mpftp.cli import TcpClient, _emit_error_envelope

try:
    from mpremote.transport import TransportError
except ImportError:  # the suite runs without a board, and may without mpremote
    class TransportError(Exception):
        pass


def _load_sidecar():
    from mpftp import sidecar

    return sidecar


PRINTED = b"boot ok\r\nstep 1\r\nstep 2\r\n"


class HangingBoard:
    """Fake raw-REPL transport whose script prints ``printed`` and then never
    sends the EOF that ends a follow (a UI loop, a blocking read, a hang).

    Mirrors ``mpremote.transport_serial.SerialTransport.exec``: with a
    ``data_consumer`` every byte is handed over as it arrives, the EOF byte
    included, and the return value carries nothing; without one the bytes are
    accumulated in a local that the timeout throws away.
    """

    in_raw_repl = True

    def __init__(self, printed: bytes, *, finishes: bool = False) -> None:
        self.printed = printed
        self.finishes = finishes

    def exec(self, command, data_consumer=None):
        stream = self.printed + (b"\x04" if self.finishes else b"")
        if data_consumer is not None:
            for i in range(len(stream)):
                data_consumer(stream[i : i + 1])
        if not self.finishes:
            raise TransportError("timeout waiting for first EOF reception")
        return b"" if data_consumer is not None else self.printed


class RunFollowTimeoutTests(unittest.TestCase):
    """The follow loop behind ``run --follow`` (Session._run_after_clean)."""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_sidecar()

    def _session(self, board):
        session = self.mod.Session()
        session.transport = board
        session.device = session.last_device = "COM99"
        return session

    def test_timeout_keeps_what_the_board_printed(self):
        board = HangingBoard(PRINTED)
        session = self._session(board)
        with mock.patch.object(self.mod, "_notify") as notify, mock.patch.object(
            session, "_take_control_resilient", return_value=board
        ), self.assertRaises(RuntimeError) as ctx:
            session._run_after_clean("import main", follow=True)
        err = ctx.exception
        self.assertIn("timeout waiting for first EOF", str(err))
        # The last line printed points at where the program actually stopped.
        self.assertEqual(getattr(err, "partial_output", None), PRINTED.decode())
        # The wedged handle is still released so Connect/Resume can reclaim it.
        self.assertIsNone(session.transport)
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0], "transport_dead")

    def test_finished_script_output_is_unchanged(self):
        board = HangingBoard(PRINTED, finishes=True)
        session = self._session(board)
        with mock.patch.object(session, "_take_control_resilient", return_value=board):
            result = session._run_after_clean("import main", follow=True)
        self.assertEqual(result, {"output": PRINTED.decode(), "followed": True})


class SidecarErrorReplyTests(unittest.TestCase):
    """The sidecar's ``{"type": "error"}`` reply for a handler exception."""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_sidecar()

    def _reply_for(self, exc):
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                raise exc
            except Exception as e:
                self.mod._error_from_exception(7, e)
        return json.loads(buf.getvalue())

    def test_follow_timeout_reply_carries_partial_output(self):
        exc = self.mod.FollowTimeoutError("timeout waiting for first EOF reception", "boot ok\n")
        reply = self._reply_for(exc)
        self.assertEqual(reply["type"], "error")
        self.assertEqual(reply["id"], 7)
        self.assertIn("timeout waiting for first EOF", reply["error"])
        self.assertEqual(reply["partialOutput"], "boot ok\n")

    def test_other_errors_have_no_partial_output_key(self):
        reply = self._reply_for(RuntimeError("not connected"))
        self.assertEqual(reply["error"], "not connected")
        self.assertNotIn("partialOutput", reply)


class ClientAndCliEnvelopeTests(unittest.TestCase):
    """RPC client exception and the CLI's stdout envelope."""

    def test_tcp_error_reply_partial_output_reaches_the_exception(self):
        reply = {
            "type": "error",
            "id": 1,
            "error": "timeout waiting for first EOF reception",
            "partialOutput": "boot ok\n",
        }
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        host, port = server.getsockname()

        def serve():
            conn, _ = server.accept()
            conn.recv(65536)
            conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
            conn.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        try:
            client = TcpClient(host, port)
            with self.assertRaises(RuntimeError) as ctx:
                client.call("run_script", {"source": "import main", "follow": True})
            self.assertIn("timeout waiting for first EOF", str(ctx.exception))
            self.assertEqual(getattr(ctx.exception, "partial_output", None), "boot ok\n")
        finally:
            server.close()
            t.join(timeout=2)

    def test_cli_envelope_emits_partial_output_on_stdout(self):
        from mpftp.cli import RpcError

        buf = io.StringIO()
        with redirect_stdout(buf):
            _emit_error_envelope(
                RpcError("timeout waiting for first EOF reception", partial_output="boot ok\n")
            )
        envelope = json.loads(buf.getvalue())
        self.assertEqual(envelope["ok"], False)
        self.assertIn("timeout waiting for first EOF", envelope["error"])
        self.assertEqual(envelope["partialOutput"], "boot ok\n")

    def test_cli_envelope_without_partial_output_is_unchanged(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            _emit_error_envelope(RuntimeError("hash mismatch: expected aa, got bb"))
        envelope = json.loads(buf.getvalue())
        self.assertEqual(envelope, {"ok": False, "error": "hash mismatch: expected aa, got bb"})


if __name__ == "__main__":
    unittest.main()
