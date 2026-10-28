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
from contextlib import redirect_stderr, redirect_stdout
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

    Mirrors ``mpremote.transport_serial.SerialTransport``: with a
    ``data_consumer`` every byte is handed over as it arrives, the EOF byte
    included, and the return value carries nothing; without one the bytes are
    accumulated in a local that the timeout throws away. ``exec`` is
    ``exec_raw`` at its fixed 10 s, as in mpremote.
    """

    in_raw_repl = True

    def __init__(self, printed: bytes, *, finishes: bool = False) -> None:
        self.printed = printed
        self.finishes = finishes

    def exec_raw(self, command, timeout=10, data_consumer=None):
        stream = self.printed + (b"\x04" if self.finishes else b"")
        if data_consumer is not None:
            for i in range(len(stream)):
                data_consumer(stream[i : i + 1])
        if not self.finishes:
            raise TransportError("timeout waiting for first EOF reception")
        return (b"" if data_consumer is not None else self.printed), b""

    def exec(self, command, data_consumer=None):
        ret, _ = self.exec_raw(command, data_consumer=data_consumer)
        return ret


class QuietBoard:
    """Fake raw-REPL transport whose script prints ``before``, works quietly
    for ``quiet`` seconds, then prints ``after`` and ends (mpftp#172).

    Time is modelled rather than slept: mpremote's follow gives up when no
    byte arrives for ``timeout`` seconds (``None`` waits forever), so the
    follow times out exactly when ``quiet`` reaches it. ``timeouts`` records
    the limit each follow was given. ``exec`` is mpremote's: ``exec_raw`` at
    its fixed 10 s, raising when the script wrote to stderr.
    """

    in_raw_repl = True

    def __init__(self, before: bytes, quiet: float, after: bytes, err: bytes = b"") -> None:
        self.before = before
        self.quiet = quiet
        self.after = after
        self.err = err
        self.timeouts: list = []

    def exec_raw(self, command, timeout=10, data_consumer=None):
        self.timeouts.append(timeout)
        out = b""

        def feed(data):
            nonlocal out
            if data_consumer is not None:
                for i in range(len(data)):
                    data_consumer(data[i : i + 1])
            else:
                out += data

        feed(self.before)
        if timeout is not None and self.quiet >= timeout:
            raise TransportError("timeout waiting for first EOF reception")
        feed(self.after + b"\x04")
        return (b"" if data_consumer is not None else out[:-1]), self.err

    def exec(self, command, data_consumer=None):
        ret, err = self.exec_raw(command, data_consumer=data_consumer)
        if err:
            from mpremote.transport import TransportExecError

            raise TransportExecError(ret, err.decode())
        return ret


QUIET_SCRIPT = 'import time\nprint("start")\ntime.sleep(20)\nprint("done")\n'


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


class RunFollowTimeoutOptionTests(unittest.TestCase):
    """``run --follow --timeout SECONDS`` through the sidecar's RPC (mpftp#172).

    The board prints "start", works quietly for 20 s, then prints "done": the
    script from the issue, which used to fail at mpremote's fixed 10 s.
    """

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_sidecar()

    def _call(self, board, method="run_script", **params):
        session = self.mod.Session()
        session.transport = board
        session.device = session.last_device = "COM99"
        self.session = session
        params.setdefault("source", QUIET_SCRIPT)
        if method == "run_path":
            params = {k: v for k, v in params.items() if k != "source"}
            params["path"] = "/quiet.py"
        with mock.patch.object(self.mod, "SESSION", session), mock.patch.object(
            self.mod, "_notify"
        ), mock.patch.object(session, "_take_control_resilient", return_value=board):
            return self.mod.METHODS[method]({"follow": True, **params})

    def test_quiet_script_longer_than_the_old_limit_finishes(self):
        board = QuietBoard(b"start\r\n", 20, b"done\r\n")
        result = self._call(board, timeout=30)
        self.assertEqual(result, {"output": "start\r\ndone\r\n", "followed": True})
        self.assertEqual(board.timeouts, [30.0])

    def test_run_path_takes_the_timeout_too(self):
        board = QuietBoard(b"start\r\n", 20, b"done\r\n")
        result = self._call(board, method="run_path", timeout=30)
        self.assertEqual(result["output"], "start\r\ndone\r\n")
        self.assertEqual(result["path"], "/quiet.py")

    def test_timeout_zero_waits_until_the_script_ends(self):
        board = QuietBoard(b"start\r\n", 3600, b"done\r\n")
        result = self._call(board, timeout=0)
        self.assertEqual(result["output"], "start\r\ndone\r\n")
        # None is mpremote's "no limit" for its read loop.
        self.assertEqual(board.timeouts, [None])

    def test_default_is_still_ten_seconds(self):
        board = QuietBoard(b"start\r\n", 20, b"done\r\n")
        with self.assertRaises(self.mod.FollowTimeoutError) as ctx:
            self._call(board)
        self.assertEqual(board.timeouts, [10.0])
        self.assertEqual(ctx.exception.partial_output, "start\r\n")
        # A script that finishes inside the default is unaffected.
        board = QuietBoard(b"start\r\n", 2, b"done\r\n")
        self.assertEqual(self._call(board)["output"], "start\r\ndone\r\n")

    def test_timeout_error_names_the_option(self):
        board = QuietBoard(b"start\r\n", 20, b"done\r\n")
        with self.assertRaises(self.mod.FollowTimeoutError) as ctx:
            self._call(board, timeout=15)
        msg = str(ctx.exception)
        self.assertIn("timeout waiting for first EOF", msg)
        self.assertIn("--timeout SECONDS", msg)
        self.assertIn("--timeout 0", msg)
        self.assertIn("printed nothing for 15 s", msg)
        self.assertEqual(ctx.exception.partial_output, "start\r\n")
        # The wedged handle is still released so Connect/Resume can reclaim it.
        self.assertIsNone(self.session.transport)

    def test_default_timeout_error_says_ten_seconds(self):
        board = QuietBoard(b"start\r\n", 20, b"done\r\n")
        with self.assertRaises(self.mod.FollowTimeoutError) as ctx:
            self._call(board)
        self.assertIn("printed nothing for 10 s", str(ctx.exception))
        self.assertIn("--timeout", str(ctx.exception))

    def test_script_error_is_raised_as_before(self):
        from mpremote.transport import TransportExecError

        board = QuietBoard(b"start\r\n", 0, b"", err=b"ZeroDivisionError: divide by zero\r\n")
        with self.assertRaises(TransportExecError) as ctx:
            self._call(board, timeout=30)
        self.assertIn("ZeroDivisionError", str(ctx.exception))

    def test_negative_timeout_is_refused(self):
        with self.assertRaises(ValueError):
            self.mod.follow_timeout(-1)
        self.assertEqual(self.mod.follow_timeout(None), 10.0)
        self.assertIsNone(self.mod.follow_timeout(0))
        self.assertEqual(self.mod.follow_timeout("2.5"), 2.5)


class QuietPort:
    """A serial port whose raw REPL runs one script: print ``start``, go quiet
    for ``gap`` real seconds, print ``done``. Enough of pyserial for mpftp's
    transport, so the follow runs through mpremote's real read loop."""

    def __init__(self, gap: float) -> None:
        self.gap = gap
        self._lock = threading.Lock()
        self._out: list = [(0.0, b">")]  # (time it is sent, bytes)
        self._in = b""

    def _ready(self) -> bytes:
        import time

        now = time.monotonic()
        return b"".join(data for at, data in self._out if at <= now)

    def inWaiting(self) -> int:
        with self._lock:
            return len(self._ready())

    def read(self, n: int = 1) -> bytes:
        import time

        deadline = time.monotonic() + 1.0
        while True:
            with self._lock:
                now = time.monotonic()
                ready = [(at, d) for at, d in self._out if at <= now]
                if ready or now >= deadline:
                    break
            time.sleep(0.005)
        with self._lock:
            got = b""
            while self._out and self._out[0][0] <= now and len(got) < n:
                at, data = self._out.pop(0)
                take = data[: n - len(got)]
                got += take
                if len(take) < len(data):
                    self._out.insert(0, (at, data[len(take) :]))
            return got

    def write(self, data: bytes) -> int:
        import time

        with self._lock:
            self._in += data
            if self._in.endswith(b"\x04"):
                now = time.monotonic()
                self._out += [
                    (now, b"OK"),
                    (now, b"start\r\n"),
                    (now + self.gap, b"done\r\n\x04\x04>"),
                ]
                self._in = b""
        return len(data)

    def flushInput(self) -> None:
        pass

    def close(self) -> None:
        pass


class RealReadLoopTests(unittest.TestCase):
    """The limit reaches the transport's own read loop, not just the call."""

    def _transport(self, gap):
        from mpftp import rawpaste

        cls = rawpaste._transport_class()
        t = cls.__new__(cls)
        t.in_raw_repl = True
        t.use_raw_paste = False
        t.device_name = "COM98"
        t.mounted = False
        t.is_pty = False
        t.paced = None
        t.serial = QuietPort(gap)
        return t

    def test_short_limit_times_out_and_keeps_output(self):
        sidecar = _load_sidecar()
        with self.assertRaises(sidecar.FollowTimeoutError) as ctx:
            sidecar.exec_follow(self._transport(1.0), "x", 0.3)
        self.assertEqual(ctx.exception.partial_output, "start\r\n")

    def test_longer_limit_and_zero_wait_for_done(self):
        sidecar = _load_sidecar()
        self.assertEqual(sidecar.exec_follow(self._transport(1.0), "x", 3.0), "start\r\ndone\r\n")
        self.assertEqual(sidecar.exec_follow(self._transport(1.0), "x", None), "start\r\ndone\r\n")


class RunTimeoutCliTests(unittest.TestCase):
    """``mpftp run --timeout`` on the command line."""

    def _run(self, argv, client=None):
        from mpftp import cli

        client = client or mock.Mock(spec=["call", "close"])
        client.call.return_value = {"output": "", "followed": True}
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        script = f"{tmp.name}/quiet.py"
        with open(script, "w", encoding="utf-8") as f:
            f.write(QUIET_SCRIPT)
        buf = io.StringIO()
        with mock.patch.object(cli, "get_client", return_value=(client, "sidecar")), mock.patch.object(
            cli, "ensure_device"
        ), redirect_stdout(buf):
            try:
                cli.main(["run", script, "-d", "COM99", *argv])
            except SystemExit as e:
                return client, buf.getvalue(), e.code
        return client, buf.getvalue(), 0

    def test_timeout_is_sent_with_follow(self):
        client, _, code = self._run(["--follow", "--timeout", "30"])
        self.assertEqual(code, 0)
        client.call.assert_called_once_with(
            "run_script", {"source": QUIET_SCRIPT, "follow": True, "timeout": 30.0}
        )

    def test_zero_is_sent_as_zero(self):
        client, _, _ = self._run(["--follow", "--timeout", "0"])
        self.assertEqual(client.call.call_args.args[1]["timeout"], 0.0)

    def test_no_timeout_sends_what_it_always_did(self):
        client, _, _ = self._run(["--follow"])
        client.call.assert_called_once_with("run_script", {"source": QUIET_SCRIPT, "follow": True})

    def test_timeout_without_follow_is_an_error(self):
        client, text, code = self._run(["--timeout", "30"])
        self.assertEqual(code, 1)
        self.assertIn("--timeout only applies with --follow", json.loads(text)["error"])
        client.call.assert_not_called()

    def test_negative_timeout_is_refused_by_the_parser(self):
        with redirect_stderr(io.StringIO()):
            _, _, code = self._run(["--follow", "--timeout", "-1"])
        self.assertEqual(code, 2)

    def test_tcp_session_waits_as_long_as_the_sidecar_does(self):
        client = TcpClient("127.0.0.1", 9)
        client.call = mock.Mock(return_value={"output": "", "followed": True})
        self._run(["--follow", "--timeout", "0"], client=client)
        self.assertIsNone(client.call_timeout)
        self.assertEqual(TcpClient.call_timeout, 120)


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
