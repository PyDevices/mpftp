"""Verification hashes on the board, CircuitPython included (mpftp#183)."""

import hashlib
import unittest

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
        exec(code, self.ns)

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


if __name__ == "__main__":
    unittest.main()
