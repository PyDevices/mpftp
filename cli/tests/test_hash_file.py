# SPDX-FileCopyrightText: 2026 Brad Barnett
#
# SPDX-License-Identifier: MIT

"""Verification hashes on the board, CircuitPython included (mpftp#183)."""

import hashlib
import io
import unittest
from contextlib import redirect_stdout

try:
    from mpremote.transport import TransportExecError

    HAVE_MPREMOTE = True
except ImportError:
    HAVE_MPREMOTE = False


class _Board:
    """Runs the hashing code mpftp sends against a hashlib of a given shape."""

    def __init__(self, files, board_hashlib):
        self.files = files
        self.board_hashlib = board_hashlib
        self.ns = {}
        self.reads = 0

    def exec(self, code):
        if self.board_hashlib is None and "import hashlib" in code:
            raise TransportExecError(1, "ImportError: no module named 'hashlib'")
        code = code.replace("import hashlib\n", "")
        files = self.files

        def _open(path, mode):
            import io

            return io.BytesIO(files[path])

        self.ns.update(hashlib=self.board_hashlib, open=_open)
        out = io.StringIO()
        with redirect_stdout(out):
            exec(code, self.ns)
        self.output = out.getvalue()
        return self.output.encode()

    def eval(self, expr):
        return eval(expr, self.ns)

    def fs_readfile(self, path, chunk_size=2048, progress_callback=None):
        self.reads += 1
        return bytearray(self.files[path])


class _CircuitPythonHashlib:
    """CircuitPython's shape: new() only."""

    @staticmethod
    def new(name, data=b""):
        return hashlib.new(name, data)


@unittest.skipUnless(HAVE_MPREMOTE, "mpremote not installed")
class HashFileTests(unittest.TestCase):
    DATA = bytes(range(256)) * 80 + b"tail"

    def _hash(self, board_hashlib):
        from mpftp.rawpaste import hash_file

        board = _Board({"/f.bin": self.DATA}, board_hashlib)
        digest = hash_file(board, "/f.bin", "sha256")
        self.assertEqual(digest, hashlib.sha256(self.DATA).digest())
        return board.reads

    def test_micropython_hashes_on_the_board(self):
        self.assertEqual(self._hash(hashlib), 0)

    def test_circuitpython_hashes_on_the_board(self):
        self.assertEqual(self._hash(_CircuitPythonHashlib), 0)

    def test_board_without_hashlib_reads_once(self):
        self.assertEqual(self._hash(None), 1)

    def test_a_long_hash_keeps_the_board_talking(self):
        # mpremote gives up after 10 s with no byte from the board; a big
        # file on an SD card took longer than that to hash (mpftp#202).
        from mpftp.rawpaste import hash_file

        size = 8_589_932
        data = bytes(range(256)) * (size // 256) + b"x" * (size % 256)
        board = _Board({"/sd/f.wav": data}, hashlib)
        self.assertEqual(hash_file(board, "/sd/f.wav", "sha256"), hashlib.sha256(data).digest())
        self.assertGreaterEqual(len(board.output), size // 65536)


if __name__ == "__main__":
    unittest.main()
