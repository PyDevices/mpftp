"""romfs build is host-only; mpremote still requires a State with did_action()."""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from mpftp.sidecar import Session


class RomfsBuildTests(unittest.TestCase):
    def test_passes_a_state_with_did_action_not_none(self):
        seen: dict = {}

        def _do_romfs_build(state, args):
            seen["state"] = state
            state.did_action()
            Path(args.output).write_bytes(b"ROMFS")
            print(f"Writing 5 bytes to output file {args.output}")

        mpremote = types.ModuleType("mpremote")
        commands = types.ModuleType("mpremote.commands")
        commands._do_romfs_build = _do_romfs_build
        mpremote.commands = commands

        session = Session()
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "romdir"
            src.mkdir()
            (src / "hello.txt").write_text("hi\n", encoding="utf-8")
            out = Path(td) / "out.romfs"
            with mock.patch.dict(
                sys.modules, {"mpremote": mpremote, "mpremote.commands": commands}
            ):
                result = session.romfs_build(str(src), output=str(out), mpy=False)
            self.assertIsNotNone(seen.get("state"))
            self.assertTrue(callable(seen["state"].did_action))
            self.assertEqual(result["size"], 5)
            self.assertEqual(result["output_file"], str(out))
            self.assertTrue(out.is_file())


if __name__ == "__main__":
    unittest.main()
