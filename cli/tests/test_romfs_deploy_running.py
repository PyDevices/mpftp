# SPDX-FileCopyrightText: 2026 Brad Barnett
#
# SPDX-License-Identifier: MIT

"""romfs deploy while modules from /rom are loaded: one scheduled step, then a reset.

The board side runs here under CPython against a fake partition, through a
fake serial link, so the host and board halves of the protocol are tested
together.
"""

from __future__ import annotations

import builtins
import io
import queue
import tempfile
import threading
import time
import types
import unittest
import zlib
from pathlib import Path
from unittest import mock

from mpftp import sidecar
from mpftp.sidecar import Session

ROM_SIZE = 64 * 1024
BLOCK = 4096


class _Reset(BaseException):
    pass


class FakeBoard:
    """A ROMFS partition, the modules the deploy script imports, and a link."""

    def __init__(self, fail_write_at=None):
        self.rom = bytearray(b"\x5a" * ROM_SIZE)
        self.erased = []
        self.writes = []
        self.reset = threading.Event()
        self.fail_write_at = fail_write_at
        self.to_board = queue.Queue()
        self.from_board = bytearray()
        self.lock = threading.Lock()

    # The board's stdin and stdout.
    def board_read(self, n):
        out = b""
        while len(out) < n:
            out += self.to_board.get(timeout=5)
        extra = out[n:]
        if extra:
            # Put back what this read didn't ask for, ahead of anything newer.
            rest = [extra]
            while not self.to_board.empty():
                rest.append(self.to_board.get())
            for r in rest:
                self.to_board.put(r)
        return out[:n]

    def board_write(self, s):
        with self.lock:
            self.from_board += s.encode() if isinstance(s, str) else s

    def rom_ioctl(self, cmd, rom_id, *args):
        if cmd == 2:
            return memoryview(self.rom)
        if cmd == 3:
            off, n = args
            self.rom[off : off + n] = b"\xff" * n
            self.erased.append((off, n))
            return 4
        if cmd == 4:
            off, data = args
            if self.fail_write_at is not None and off == self.fail_write_at:
                return -5
            self.writes.append(off)
            self.rom[off : off + len(data)] = data
            return 0
        if cmd == 5:
            return 0
        if cmd == 6:
            return BLOCK
        raise AssertionError(cmd)

    def modules(self):
        board = self

        class _Stdin:
            buffer = types.SimpleNamespace(read=board_read_wrapper(board))

        def umount(_p):
            raise OSError(22)

        def reset():
            board.reset.set()
            raise _Reset

        def schedule(fn, arg):
            fn(arg)

        class DeflateIO:
            def __init__(self, stream, _fmt, _wbits):
                self.stream = stream

            def read(self):
                return zlib.decompress(self.stream.read(), wbits=-9)

        return {
            "sys": types.SimpleNamespace(
                stdin=_Stdin,
                stdout=types.SimpleNamespace(buffer=types.SimpleNamespace(write=board.board_write)),
            ),
            "vfs": types.SimpleNamespace(rom_ioctl=board.rom_ioctl, umount=umount),
            "machine": types.SimpleNamespace(reset=reset),
            "micropython": types.SimpleNamespace(schedule=schedule),
            "time": types.SimpleNamespace(sleep_ms=lambda _ms: None),
            "binascii": __import__("binascii"),
            "io": types.SimpleNamespace(BytesIO=io.BytesIO),
            "deflate": types.SimpleNamespace(DeflateIO=DeflateIO, RAW=0),
        }

    def run_script(self, script):
        mods = self.modules()
        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name in mods:
                return mods[name]
            return real_import(name, globals, locals, fromlist, level)

        env = {"__builtins__": dict(vars(builtins), __import__=fake_import)}

        def go():
            try:
                exec(script, env)
            except _Reset:
                pass

        threading.Thread(target=go, daemon=True).start()


def board_read_wrapper(board):
    return lambda n: board.board_read(n)


class FakeSerial:
    def __init__(self, board):
        self.board = board

    def write(self, data):
        self.board.to_board.put(bytes(data))
        return len(data)

    def inWaiting(self):  # noqa: N802
        with self.board.lock:
            return len(self.board.from_board)

    def read(self, n=1):
        with self.board.lock:
            out = bytes(self.board.from_board[:n])
            del self.board.from_board[:n]
        return out


class FakeTransport:
    def __init__(self, board):
        self.board = board
        self.serial = FakeSerial(board)
        self.in_raw_repl = True

    def exec(self, code):
        return b""

    def eval(self, expr):
        answers = {
            "hasattr(vfs,'rom_ioctl')": True,
            "isinstance(dev,int) and dev<0": False,
            "hasattr(dev,'ioctl')": False,
            "len(dev)": ROM_SIZE,
            "vfs.rom_ioctl(6,0)": BLOCK,
        }
        return answers[expr]

    def exec_raw_no_follow(self, script):
        self.board.run_script(script)


def make_image(size):
    body = bytes((i * 7) & 0xFF for i in range(size - 3))
    return b"\xd2\xcd\x31" + body


class DeployWhileRunningTests(unittest.TestCase):
    def deploy(self, board, image):
        session = Session()
        session.transport = FakeTransport(board)
        closed = []
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            Session, "_require", lambda self: self.transport
        ), mock.patch.object(
            Session, "_force_close_transport", lambda self, graceful=False: closed.append(graceful)
        ):
            path = Path(td) / "app.romfs"
            path.write_bytes(image)
            result = session._romfs_deploy_and_reset(str(path), 0, True, ["appkit"])
        self.assertTrue(board.reset.wait(5), "the board was never reset")
        self.assertEqual(closed, [False])
        return result

    def test_writes_the_image_header_last_and_resets(self):
        board = FakeBoard()
        image = make_image(3 * BLOCK + 100)
        result = self.deploy(board, image)
        self.assertTrue(result["ok"], result)
        self.assertEqual(bytes(board.rom[: len(image)]), image)
        self.assertEqual(board.writes[-1], 0)
        self.assertEqual(sorted(board.writes), [0, BLOCK, 2 * BLOCK, 3 * BLOCK])
        self.assertEqual(board.erased, [(0, 4 * BLOCK)])

    def test_a_failed_write_is_reported_and_still_resets(self):
        board = FakeBoard(fail_write_at=BLOCK)
        result = self.deploy(board, make_image(2 * BLOCK))
        self.assertFalse(result["ok"])
        self.assertIn("OSError", result["error"])
        # The header chunk never went in, so nothing valid is left to mount.
        self.assertNotIn(0, board.writes)

    def test_deploy_takes_this_path_only_with_modules_from_rom(self):
        session = Session()
        session.interpreter = "micropython"
        transport = mock.Mock(in_raw_repl=True)
        transport.eval.return_value = ["appkit", "events"]
        session.transport = transport
        with mock.patch.object(Session, "_require", lambda self: transport), mock.patch.object(
            Session, "_romfs_deploy_and_reset", return_value={"ok": True}
        ) as locked, mock.patch.object(Session, "_require_micropython", lambda self, what: None):
            self.assertEqual(session.romfs_deploy("x.romfs"), {"ok": True})
        locked.assert_called_once_with("x.romfs", 0, True, ["appkit", "events"])

    def test_wifi_always_takes_this_path(self):
        session = Session()
        session.interpreter = "micropython"
        session.device = "ws://192.168.1.50"
        transport = mock.Mock(in_raw_repl=True)
        transport.eval.return_value = []
        session.transport = transport
        with mock.patch.object(Session, "_require", lambda self: transport), mock.patch.object(
            Session, "_romfs_deploy_and_reset", return_value={"ok": True}
        ) as locked, mock.patch.object(Session, "_require_micropython", lambda self, what: None):
            session.romfs_deploy("x.romfs")
        locked.assert_called_once_with("x.romfs", 0, True, [])


class MarkerReaderTests(unittest.TestCase):
    def test_a_quiet_board_is_reported(self):
        board = FakeBoard()
        with self.assertRaisesRegex(RuntimeError, "went quiet"):
            sidecar._romfs_read_marker(FakeSerial(board), b"R", 0.05)

    def test_an_error_line_is_reported(self):
        board = FakeBoard()
        board.board_write("EEXOSError(5,)\n")
        start = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, r"OSError\(5,\)"):
            sidecar._romfs_read_marker(FakeSerial(board), b"R", 5)
        self.assertLess(time.monotonic() - start, 1)


if __name__ == "__main__":
    unittest.main()
