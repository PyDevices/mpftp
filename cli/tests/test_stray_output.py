"""A board that is busy when mpftp connects over a USB-UART bridge (mpftp#98).

On a CircuitPython board whose console is a CH343 UART, every standalone
command failed with "Expecting value: line 1 column 1": opening the port
reset the board, the raw-REPL handshake missed while it booted, mpremote
printed the bytes it had read to the sidecar's stdout, and the CLI fed that
line to ``json.loads``.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr
from unittest import mock

from mpftp import rawpaste, sidecar
from mpftp.cli import SidecarClient, _sidecar_message

try:
    import mpremote  # noqa: F401

    HAVE_MPREMOTE = True
except ImportError:
    HAVE_MPREMOTE = False

BANNER = b"raw REPL; CTRL-B to exit\r\n"
# What mpremote printed when it could not enter the raw REPL on the bench.
STRAY = "b'Serial console setup\\r\\n\\x1b[2K\\x1b[0GAdafruit CircuitPython 11.0.0'\n"


class SidecarClientStrayLineTests(unittest.TestCase):
    def _client(self, lines: list[str]) -> SidecarClient:
        client = SidecarClient.__new__(SidecarClient)
        client._id = 0
        client.proc = mock.Mock()
        client.proc.stdin = mock.Mock()
        client.proc.stdout = mock.Mock()
        client.proc.stdout.readline.side_effect = [*lines, ""]
        client.proc.stderr = mock.Mock()
        client.proc.stderr.read.return_value = ""
        return client

    def test_a_line_that_is_not_json_is_shown_and_skipped(self):
        result = json.dumps({"type": "result", "id": 1, "result": {"output": "1\r\n"}}) + "\n"
        client = self._client([STRAY, result])
        err = io.StringIO()
        with redirect_stderr(err):
            got = client.call("exec", {"code": "print(1)"})
        self.assertEqual(got, {"output": "1\r\n"})
        self.assertIn("Serial console setup", err.getvalue())

    def test_json_that_is_not_an_object_is_not_a_message(self):
        with redirect_stderr(io.StringIO()):
            self.assertIsNone(_sidecar_message("42\n"))
        self.assertEqual(_sidecar_message('{"type": "notify"}\n'), {"type": "notify"})


class SidecarStdoutTests(unittest.TestCase):
    def test_a_print_inside_a_method_stays_off_the_rpc_channel(self):
        """mpremote prints what it read when the raw-REPL handshake misses."""
        channel = io.StringIO()
        request = json.dumps({"id": 1, "method": "noisy", "params": {}}) + "\n"

        def noisy(_params):
            print(STRAY, end="")
            return {"ok": True}

        saved_out, saved_rpc = sys.stdout, sidecar._rpc_out
        try:
            with mock.patch.object(sidecar, "resolve_session_id", return_value="t"), \
                    mock.patch.object(sidecar, "cleanup_stale_sidecars", return_value=[]), \
                    mock.patch.object(sidecar, "claim_sidecar_pid"), \
                    mock.patch.object(sidecar, "release_sidecar_pid"), \
                    mock.patch.object(sidecar, "SESSION"), \
                    mock.patch.dict(sidecar.METHODS, {"noisy": noisy}), \
                    mock.patch("atexit.register"), \
                    mock.patch.object(sys, "stdin", io.StringIO(request)), \
                    mock.patch.object(sys, "stderr", io.StringIO()) as err:
                sys.stdout = channel
                sidecar.main()
        finally:
            sys.stdout, sidecar._rpc_out = saved_out, saved_rpc
        lines = channel.getvalue().splitlines()
        self.assertEqual([json.loads(line)["type"] for line in lines], ["notify", "result"])
        self.assertIn("Serial console setup", err.getvalue())


class _Port:
    """pyserial's surface, recording the order its lines and open() happen in."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def __setattr__(self, name, value):
        if name in ("dtr", "rts"):
            self.events.append(f"{name}={value}")
        object.__setattr__(self, name, value)

    def open(self) -> None:
        self.events.append("open")


@unittest.skipUnless(HAVE_MPREMOTE, "mpremote not installed")
class OpenWithLinesLowTests(unittest.TestCase):
    def test_dtr_and_rts_are_low_before_the_port_opens(self):
        port = _Port()
        with mock.patch("serial.serial_for_url", return_value=port) as make:
            t = rawpaste.open_serial_transport("COM17", 115200, paced=lambda: True, lines_low=True)
        self.assertEqual(port.events, ["dtr=False", "rts=False", "open"])
        self.assertIs(t.serial, port)
        self.assertTrue(make.call_args.kwargs.get("do_not_open"))
        self.assertFalse(t.in_raw_repl)
        self.assertTrue(t.use_raw_paste)

    def test_sidecar_holds_lines_low_on_a_uart_bridge_only(self):
        ports = [
            mock.Mock(device="COM17", vid=0x1A86),
            mock.Mock(device="COM42", vid=0x303A),
        ]
        s = sidecar.Session.__new__(sidecar.Session)
        s.interpreter = None
        with mock.patch("serial.tools.list_ports.comports", return_value=ports), \
                mock.patch.object(rawpaste, "open_serial_transport") as opened:
            s._open_transport("COM17", 115200)
            s._open_transport("COM42", 115200)
        self.assertEqual([c.kwargs["lines_low"] for c in opened.call_args_list], [True, False])


@unittest.skipUnless(HAVE_MPREMOTE, "mpremote not installed")
class RawPasteRetryTests(unittest.TestCase):
    def test_a_paced_board_is_asked_for_raw_paste_every_time(self):
        cls = rawpaste._transport_class()
        t = cls.__new__(cls)
        t.use_raw_paste = False
        seen = []
        with mock.patch(
            "mpremote.transport_serial.SerialTransport.exec_raw_no_follow",
            side_effect=lambda self, cmd: seen.append(self.use_raw_paste),
            autospec=True,
        ):
            t.paced = lambda: True
            t.exec_raw_no_follow(b"x")
            t.use_raw_paste = False
            t.paced = lambda: False
            t.exec_raw_no_follow(b"x")
        self.assertEqual(seen, [True, False])


class _RawRepl:
    """A board sitting in the raw REPL, with replies to earlier pokes queued."""

    def __init__(self, pending: bytes) -> None:
        self.pending = bytearray(pending)

    def inWaiting(self) -> int:  # noqa: N802 (pyserial's name)
        return len(self.pending)

    def read(self, n: int = 1) -> bytes:
        out = bytes(self.pending[:n])
        del self.pending[:n]
        return out

    def write(self, data: bytes) -> int:
        if b"\x01" in data:
            self.pending += BANNER + b">"
        return len(data)


class _Transport:
    def __init__(self, serial: _RawRepl) -> None:
        self.serial = serial
        self.in_raw_repl = True

    def read_until(self, _n, ending, timeout_overall=None):
        data = b""
        while self.serial.pending and not data.endswith(ending):
            data += self.serial.read(1)
        return data


@unittest.skipUnless(HAVE_MPREMOTE, "mpremote not installed")
class ResyncRawReplTests(unittest.TestCase):
    def test_leaves_exactly_one_prompt(self):
        board = _RawRepl(b">\r\n>>> " + BANNER + b">")
        sidecar.Session._resync_raw_repl(_Transport(board), quiet=0.01)
        self.assertEqual(bytes(board.pending), b">")

    def test_take_control_resyncs_after_a_failed_try(self):
        from mpremote.transport import TransportError

        s = sidecar.Session.__new__(sidecar.Session)
        s.interpreter = None
        s.filesystem_warning = None
        t = _Transport(_RawRepl(b""))
        t.enter_raw_repl = mock.Mock(side_effect=[TransportError("could not enter raw repl"), None])
        with mock.patch.object(sidecar.Session, "_resync_raw_repl") as resync, \
                mock.patch.object(sidecar.Session, "_finish_clean_after_raw"), \
                mock.patch("time.sleep"):
            s._take_control(t, clean=True)
        resync.assert_called_once_with(t)

    def test_a_first_try_that_works_does_not_resync(self):
        s = sidecar.Session.__new__(sidecar.Session)
        s.interpreter = None
        s.filesystem_warning = None
        t = _Transport(_RawRepl(b""))
        t.enter_raw_repl = mock.Mock(return_value=None)
        with mock.patch.object(sidecar.Session, "_resync_raw_repl") as resync, \
                mock.patch.object(sidecar.Session, "_finish_clean_after_raw"), \
                mock.patch("time.sleep"):
            s._take_control(t, clean=True)
        resync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
