"""WebREPL client: WebSocket framing, the upgrade, the password login.

The "board" here is a thread on the far end of a socketpair that speaks the
server side the way extmod/modwebrepl.c and webrepl.py do. No board required.
"""

from __future__ import annotations

import base64
import hashlib
import socket
import struct
import threading
import time
import unittest

from mpftp import webrepl
from mpftp.webrepl import (
    OP_BINARY,
    OP_CLOSE,
    OP_PONG,
    OP_TEXT,
    FrameParser,
    WebReplAuthError,
    WebReplError,
    WebSocketSerial,
    encode_frame,
)

PASSWORD = "sekrit1"


def board_frame(opcode: int, payload: bytes) -> bytes:
    """A frame as the board writes it: final, unmasked."""
    n = len(payload)
    if n < 126:
        return bytes([0x80 | opcode, n]) + payload
    return bytes([0x80 | opcode, 126]) + struct.pack("!H", n) + payload


class FakeBoard(threading.Thread):
    """The server end of a WebREPL session, scripted per test."""

    def __init__(self, sock: socket.socket, *, password: str = PASSWORD, script=None):
        super().__init__(daemon=True)
        self.sock = sock
        self.password = password
        self.script = script or FakeBoard.default_session
        self.parser = FrameParser()
        self.received: list[tuple[int, bytes]] = []
        self.error: BaseException | None = None

    # -- helpers the scripts use ------------------------------------------

    def read_request(self) -> bytes:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(1024)
            if not chunk:
                break
            data += chunk
        return data

    def accept(self, request: bytes, key_override: bytes | None = None) -> None:
        key = None
        for line in request.split(b"\r\n"):
            name, _, value = line.partition(b":")
            if name == b"Sec-WebSocket-Key":  # modwebrepl matches it exactly
                key = value.strip()
        assert key is not None, request
        accept = base64.b64encode(hashlib.sha1(key + webrepl._WS_GUID).digest())
        self.sock.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Accept: "
            + (key_override or accept)
            + b"\r\n\r\n"
        )

    def next_frame(self) -> tuple[int, bytes]:
        while True:
            if self.received:
                return self.received.pop(0)
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("client went away")
            self.received.extend(self.parser.feed(chunk))

    def next_data_frame(self) -> tuple[int, bytes]:
        while True:
            op, payload = self.next_frame()
            if op != OP_PONG:
                return op, payload

    def send(self, opcode: int, payload: bytes) -> None:
        self.sock.sendall(board_frame(opcode, payload))

    def login(self) -> bool:
        self.accept(self.read_request())
        self.send(OP_TEXT, b"Password: ")
        typed = b""
        while not typed.endswith((b"\r", b"\n")):
            op, payload = self.next_data_frame()
            typed += payload
        if typed.strip().decode() != self.password:
            self.send(OP_TEXT, b"\r\nAccess denied\r\n")
            self.sock.close()
            return False
        self.send(OP_TEXT, b"\r\nWebREPL connected\r\n>>> ")
        return True

    def default_session(self) -> None:
        self.login()

    def run(self) -> None:
        try:
            self.script(self)
        except BaseException as e:  # surfaced by the test via .error
            self.error = e


def connect(script=None, *, password: str = PASSWORD, **kwargs):
    """A WebSocketSerial wired to a FakeBoard; returns (ws, board)."""
    client_end, board_end = socket.socketpair()
    board = FakeBoard(board_end, script=script)
    board.start()
    ws = WebSocketSerial(
        "ws://192.0.2.1:8266",
        password,
        create_connection=lambda addr, timeout=None: client_end,
        login_timeout=2.0,
        **kwargs,
    )
    ws._test_socks = (client_end, board_end)
    return ws, board


class SessionCase(unittest.TestCase):
    def connect(self, script=None, **kwargs):
        ws, board = connect(script, **kwargs)
        for sock in ws._test_socks:
            self.addCleanup(sock.close)
        self.addCleanup(ws.close)
        return ws, board


class FramingTests(unittest.TestCase):
    def test_client_frames_are_masked_and_final(self):
        frame = encode_frame(OP_TEXT, b"hi", mask_key=b"\x01\x02\x03\x04")
        self.assertEqual(frame[0], 0x81)
        self.assertEqual(frame[1], 0x80 | 2)
        self.assertEqual(frame[2:6], b"\x01\x02\x03\x04")
        self.assertEqual(frame[6:], bytes([ord("h") ^ 1, ord("i") ^ 2]))

    def test_masked_frame_round_trips_through_the_parser(self):
        payload = bytes(range(256)) * 3
        frame = encode_frame(OP_BINARY, payload)
        self.assertEqual(frame[1] & 0x7F, 126)  # 16-bit length form
        self.assertEqual(FrameParser().feed(frame), [(OP_BINARY, payload)])

    def test_frames_the_board_cannot_parse_are_refused(self):
        # modwebsocket.c closes the connection on the 64-bit length form.
        with self.assertRaises(ValueError):
            encode_frame(OP_TEXT, b"x" * 0x10000)

    def test_parser_reassembles_byte_by_byte_and_splits_runs(self):
        stream = board_frame(OP_TEXT, b"OK") + board_frame(OP_TEXT, b"x" * 300)
        parser = FrameParser()
        frames = []
        for i in range(len(stream)):
            frames += parser.feed(stream[i : i + 1])
        self.assertEqual(frames, [(OP_TEXT, b"OK"), (OP_TEXT, b"x" * 300)])
        self.assertEqual(parser.pending, 0)
        self.assertEqual(FrameParser().feed(stream), frames)

    def test_parser_reports_a_header_waiting_for_its_payload(self):
        parser = FrameParser()
        self.assertEqual(parser.feed(b"\x81\x05"), [])
        self.assertEqual(parser.pending, 2)


class DeviceStringTests(unittest.TestCase):
    def test_default_port_is_8266(self):
        self.assertEqual(webrepl.parse_device("ws://10.0.0.5"), ("10.0.0.5", 8266, "/"))
        self.assertEqual(webrepl.parse_device("WS://board.local:9000/"), ("board.local", 9000, "/"))

    def test_serial_ports_are_not_network_devices(self):
        for dev in ("COM4", "/dev/ttyACM0", "", None):
            self.assertFalse(webrepl.is_network_device(dev))
        self.assertTrue(webrepl.is_network_device("ws://1.2.3.4"))

    def test_wss_and_credentials_in_the_address_are_refused(self):
        with self.assertRaisesRegex(ValueError, "no TLS"):
            webrepl.parse_device("wss://10.0.0.5")
        with self.assertRaisesRegex(ValueError, "password in the address"):
            webrepl.parse_device("ws://:pw@10.0.0.5")

    def test_password_rules(self):
        with self.assertRaisesRegex(WebReplAuthError, "no WebREPL password"):
            webrepl.check_password("", "ws://x")
        with self.assertRaisesRegex(WebReplAuthError, "1 to 9"):
            webrepl.check_password("0123456789", "ws://x")
        self.assertEqual(webrepl.check_password("012345678", "ws://x"), "012345678")


class HandshakeTests(SessionCase):
    def test_login_then_bytes_flow_both_ways(self):
        def script(board):
            board.login()
            op, payload = board.next_data_frame()
            assert (op, payload) == (OP_TEXT, b"\r\x03"), (op, payload)
            board.send(OP_TEXT, b"raw REPL; CTRL-B to exit\r\n>")
            board.send(OP_BINARY, b"WB\x00\x00")

        ws, board = self.connect(script)
        ws.open()
        ws.write(b"\r\x03")
        self.assertEqual(ws.read(27), b"raw REPL; CTRL-B to exit\r\n>")
        # Binary frames are the file channel; they never mix into the REPL.
        self.assertEqual(ws.read_binary(4, timeout=2), b"WB\x00\x00")
        self.assertEqual(ws.inWaiting(), 0)
        board.join(2)
        self.assertIsNone(board.error)
        ws.close()

    def test_wrong_password_fails_fast_and_says_so(self):
        ws, board = self.connect(password="nope")
        t0 = time.monotonic()
        with self.assertRaisesRegex(WebReplAuthError, "rejected the WebREPL password"):
            ws.open()
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertFalse(ws.is_open)

    def test_second_client_rejected_before_the_prompt(self):
        def script(board):  # webrepl.py: "Concurrent WebREPL connection ... rejected"
            board.accept(board.read_request())
            board.sock.close()

        ws, _ = self.connect(script)
        with self.assertRaisesRegex(WebReplError, "Another WebREPL client"):
            ws.open()

    def test_a_plain_web_server_is_not_mistaken_for_webrepl(self):
        def script(board):
            board.read_request()
            board.sock.sendall(b"HTTP/1.0 200 OK\r\n\r\n<base href=...>")
            board.sock.close()

        ws, _ = self.connect(script)
        with self.assertRaisesRegex(WebReplError, "not a WebREPL server"):
            ws.open()

    def test_a_wrong_accept_key_is_refused(self):
        def script(board):
            board.accept(board.read_request(), key_override=b"AAAA")

        ws, _ = self.connect(script)
        with self.assertRaisesRegex(WebReplError, "wrong key"):
            ws.open()

    def test_silent_board_times_out_instead_of_hanging(self):
        def script(board):
            board.accept(board.read_request())
            time.sleep(3)  # never sends the prompt

        ws, _ = self.connect(script)
        t0 = time.monotonic()
        with self.assertRaisesRegex(WebReplError, "no the password prompt|password prompt"):
            ws.open()
        self.assertLess(time.monotonic() - t0, 3.0)


class SessionTests(SessionCase):
    def test_a_header_sent_alone_is_acked_with_a_pong(self):
        # lwIP's Nagle sends the 2-byte header, then holds the payload until
        # that segment is ACKed. The client answers a lone header at once.
        def script(board):
            board.login()
            board.sock.sendall(b"\x81\x02")
            op, payload = board.next_frame()
            assert op == OP_PONG and payload == b"", (op, payload)
            board.sock.sendall(b"OK")

        ws, board = self.connect(script)
        ws.open()
        self.assertEqual(ws.read(2), b"OK")
        board.join(2)
        self.assertIsNone(board.error)

    def test_in_waiting_counts_new_bytes_behind_unread_ones(self):
        # The sidecar watches rx_total for the board's answer to Ctrl-C. A
        # leftover byte from an earlier reply must not stop the socket being
        # read, or a live board looks busy (seen on the P4, 2026-09-24).
        got = threading.Event()

        def script(board):
            board.login()
            board.send(OP_TEXT, b">")
            op, payload = board.next_data_frame()
            assert payload == b"\r\x03", payload
            board.send(OP_TEXT, b"\r\n>>> ")
            got.wait(2)

        ws, board = self.connect(script)
        ws.open()
        ws.read(0)
        deadline = time.monotonic() + 2
        while not ws.inWaiting() and time.monotonic() < deadline:
            time.sleep(0.01)
        before = ws.rx_total
        ws.write(b"\r\x03")
        while ws.rx_total == before and time.monotonic() < deadline:
            ws.inWaiting()
            time.sleep(0.01)
        got.set()
        self.assertEqual(ws.rx_total, before + 6)
        board.join(2)
        self.assertIsNone(board.error)

    def test_ping_is_answered(self):
        def script(board):
            board.login()
            board.send(0x9, b"hb")
            op, payload = board.next_frame()
            assert (op, payload) == (OP_PONG, b"hb"), (op, payload)
            board.send(OP_TEXT, b"!")

        ws, board = self.connect(script)
        ws.open()
        self.assertEqual(ws.read(1), b"!")
        board.join(2)
        self.assertIsNone(board.error)

    def test_a_dropped_connection_reads_as_a_dead_port(self):
        def script(board):
            board.login()
            board.send(OP_CLOSE, b"")
            board.sock.close()

        ws, _ = self.connect(script)
        ws.open()
        with self.assertRaises(WebReplError) as cm:
            ws.read(1)
        # The sidecar's recovery keys on this wording, as it does for a COM port.
        self.assertIn("port is closed", str(cm.exception))
        with self.assertRaises(WebReplError):
            ws.write(b"x")

    def test_a_quiet_blocking_read_gives_up(self):
        def script(board):
            board.login()
            time.sleep(2)

        ws, _ = self.connect(script)
        ws.open()
        old = webrepl.STALL_TIMEOUT
        webrepl.STALL_TIMEOUT = 0.5
        try:
            with self.assertRaisesRegex(WebReplError, "stalled"):
                ws.read(1)
        finally:
            webrepl.STALL_TIMEOUT = old

    def test_long_writes_are_split_into_frames_the_board_accepts(self):
        data = bytes(range(256)) * 20  # 5 KiB

        def script(board):
            board.login()
            got = b""
            while len(got) < len(data):
                op, payload = board.next_data_frame()
                assert op == OP_TEXT and len(payload) <= webrepl.MAX_FRAME
                got += payload
            assert got == data
            board.send(OP_TEXT, b"done")

        ws, board = self.connect(script)
        ws.open()
        ws.write(data)
        self.assertEqual(ws.read(4), b"done")
        board.join(2)
        self.assertIsNone(board.error)


try:
    import mpremote  # noqa: F401

    HAVE_MPREMOTE = True
except ImportError:
    HAVE_MPREMOTE = False


@unittest.skipUnless(HAVE_MPREMOTE, "mpremote not installed")
class TransportTests(SessionCase):
    """The mpremote subclass: plain raw REPL exec and binary PUT."""

    def _transport(self, script):
        ws, board = self.connect(script)
        ws.open()
        return webrepl._transport_class()(ws), board

    def test_exec_sends_the_command_in_one_piece_and_follows(self):
        def script(board):
            board.login()
            board.send(OP_TEXT, b">")
            got = b""
            while not got.endswith(b"\x04"):
                got += board.next_data_frame()[1]
            assert got == b"print(1)\x04", got
            board.send(OP_TEXT, b"OK")
            board.send(OP_TEXT, b"1\r\n\x04\x04>")

        t, board = self._transport(script)
        t.in_raw_repl = True
        self.assertEqual(t.exec("print(1)"), b"1\r\n")
        board.join(2)
        self.assertIsNone(board.error)

    def test_put_uses_the_webrepl_file_protocol(self):
        data = bytes(range(256)) * 10
        name = b"/lib/x.bin"

        def exec_ok(board, expect: bytes, output: bytes = b""):
            board.send(OP_TEXT, b">")
            got = b""
            while not got.endswith(b"\x04"):
                got += board.next_data_frame()[1]
            assert expect in got, got
            board.send(OP_TEXT, b"OK" + output + b"\x04\x04")

        def script(board):
            board.login()
            exec_ok(board, b"open('/lib/x.bin','wb').close()")
            op, hdr = board.next_data_frame()
            assert op == OP_BINARY and len(hdr) == 82, (op, len(hdr))
            sig, kind, _, _, size, nlen, fname = struct.unpack("<2sBBQLH64s", hdr)
            assert (sig, kind, size, fname[:nlen]) == (b"WA", 1, len(data), name)
            board.send(OP_BINARY, b"WB\x00\x00")
            got = b""
            while len(got) < size:
                op, payload = board.next_data_frame()
                assert op == OP_BINARY
                got += payload
            assert got == data
            board.send(OP_BINARY, b"WB\x00\x00")
            exec_ok(board, b"stat('/lib/x.bin')", b"%d\r\n" % len(data))

        t, board = self._transport(script)
        t.in_raw_repl = True
        t.fs_writefile("/lib/x.bin", data)
        board.join(2)
        self.assertIsNone(board.error)


if __name__ == "__main__":
    unittest.main()
