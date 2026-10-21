"""get, put, cp and hash keep every byte of a binary file (null bytes included).

A model file (.espdl) is full of zeros and every other byte value. The board
sends file content as a printed ``repr`` that mpremote parses on the host, so
any step that handles the content as text, or passes it through ``eval`` or
``compile``, fails on a null byte ("source code string cannot contain null
bytes") or silently changes it. These tests run the real sidecar and CLI
commands against a stand-in board that executes mpremote's own board-side
code under CPython, so no hardware is needed.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import io
import os
import random
import tempfile
import unittest
from pathlib import Path

from mpftp import cli, sidecar

try:
    from mpremote.transport import Transport

    HAVE_MPREMOTE = True
except ImportError:  # the sidecar's own tests need mpremote; skip without it
    Transport = object  # type: ignore[assignment,misc]
    HAVE_MPREMOTE = False


def binary_blob() -> bytes:
    """All 256 byte values, runs of zeros, and noise, not a multiple of 256 long."""
    rng = random.Random(1)
    noise = bytes(rng.randrange(256) for _ in range(5000))
    return (
        bytes(range(256))
        + bytes(300)
        + bytes(range(255, -1, -1)) * 3
        + noise[:2500]
        + bytes(1000)
        + noise[2500:]
        + b"\x04\x03\x01\x00\x00"
    )


class PythonBoard(Transport):
    """A raw-REPL board that runs what mpremote sends, under CPython.

    ``fs_readfile``, ``fs_writefile``, ``fs_hashfile`` and ``fs_listdir`` are
    mpremote's own, so the host side of every transfer is the real code.
    """

    in_raw_repl = True

    def __init__(self) -> None:
        self.ns: dict = {}

    def exec(self, command, data_consumer=None):
        if isinstance(command, bytes):
            command = command.decode()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(compile(command, "<board>", "exec"), self.ns)
        out = buf.getvalue().encode()
        if data_consumer:
            data_consumer(out)
        return out

    def eval(self, expression, parse=True):
        if expression.startswith("os.stat("):  # a board's stat is a plain tuple
            expression = "tuple(%s)" % expression
        out = self.exec("print(repr({}))".format(expression)).strip()
        return ast.literal_eval(out.decode())


class LocalClient:
    """The CLI's RPC client, calling the session in this process."""

    def __init__(self, session: sidecar.Session) -> None:
        self.session = session

    def call(self, method: str, params: dict | None = None):
        return getattr(self.session, method)(**(params or {}))


@unittest.skipUnless(HAVE_MPREMOTE, "mpremote not installed")
class BinaryRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        # The stand-in board's "filesystem" is a folder of the host's own.
        self.remote_dir = self.dir / "board" / "models"
        self.remote_dir.mkdir(parents=True)
        self.remote = str(self.remote_dir / "net.espdl")
        self.data = binary_blob()
        self.sha = hashlib.sha256(self.data).hexdigest()

        self.session = sidecar.Session()
        self.session.transport = PythonBoard()
        self.session.device = "COM4"
        self.session.interpreter = "micropython"
        self.client = LocalClient(self.session)

        self._orig_get_client = cli.get_client
        self._orig_ensure = cli.ensure_device
        cli.get_client = lambda prefer_rpc=True: (self.client, "tcp")
        cli.ensure_device = lambda *a, **k: None
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        cli.get_client = self._orig_get_client
        cli.ensure_device = self._orig_ensure

    def _ns(self, **kw) -> argparse.Namespace:
        base = dict(device=None, baud=115200, recursive=False, verify=True, mpy=False)
        base.update(kw)
        return argparse.Namespace(**base)

    def _run(self, fn, **kw) -> None:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            fn(self._ns(**kw))

    def _remote_sha(self, path: str | None = None) -> str:
        return hashlib.sha256(Path(path or self.remote).read_bytes()).hexdigest()

    def test_the_blob_has_every_byte_value_and_null_runs(self):
        self.assertEqual(set(self.data), set(range(256)))
        self.assertIn(bytes(1000), self.data)

    def test_put_then_get_is_byte_exact(self):
        src = self.dir / "in.espdl"
        src.write_bytes(self.data)
        self._run(cli.cmd_put, local=str(src), remote=self.remote)
        self.assertEqual(self._remote_sha(), self.sha)

        dst = self.dir / "out.espdl"
        self._run(cli.cmd_get, local=str(dst), remote=self.remote)
        self.assertEqual(hashlib.sha256(dst.read_bytes()).hexdigest(), self.sha)

    def test_get_without_verify_is_byte_exact(self):
        Path(self.remote).write_bytes(self.data)
        dst = self.dir / "out.espdl"
        self._run(cli.cmd_get, local=str(dst), remote=self.remote, verify=False)
        self.assertEqual(hashlib.sha256(dst.read_bytes()).hexdigest(), self.sha)

    def test_hash_matches_sha256_of_the_file(self):
        Path(self.remote).write_bytes(self.data)
        res = self.client.call("fs_hash", {"path": self.remote, "algo": "sha256"})
        self.assertEqual(res["hash"], self.sha)

    def test_cp_board_to_host_to_board_is_byte_exact(self):
        Path(self.remote).write_bytes(self.data)
        local = self.dir / "copy.espdl"
        self._run(cli.cmd_cp, src=":" + self.remote, dest=str(local))
        self.assertEqual(hashlib.sha256(local.read_bytes()).hexdigest(), self.sha)

        again = str(self.remote_dir / "again.espdl")
        self._run(cli.cmd_cp, src=str(local), dest=":" + again)
        self.assertEqual(self._remote_sha(again), self.sha)

        same_board = str(self.remote_dir / "same.espdl")
        self._run(cli.cmd_cp, src=":" + self.remote, dest=":" + same_board)
        self.assertEqual(self._remote_sha(same_board), self.sha)

    def test_get_r_of_a_folder_is_byte_exact(self):
        Path(self.remote).write_bytes(self.data)
        (self.remote_dir / "zeros.bin").write_bytes(bytes(4096))
        out = self.dir / "models"
        self._run(cli.cmd_get, local=str(out), remote=str(self.remote_dir), recursive=True)
        self.assertEqual(hashlib.sha256((out / "net.espdl").read_bytes()).hexdigest(), self.sha)
        self.assertEqual((out / "zeros.bin").read_bytes(), bytes(4096))


if __name__ == "__main__":
    unittest.main()
