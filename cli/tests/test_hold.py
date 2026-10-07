"""``mpftp hold``: a REPL kept open across commands.

The parsing tests pin down how a reply is cut out of a stream the app also
prints into. The end-to-end tests start a real holder on a pseudo terminal
(CPython's own REPL, and Linux MicroPython when it is installed) and drive
it through the same functions the CLI calls.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mpftp import hold


class KeyTests(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(hold.key_for("com42"), "COM42")
        self.assertEqual(hold.key_for("/dev/ttyACM0"), "dev-ttyACM0")
        self.assertEqual(hold.key_for("ws://192.168.1.9:8266"), "ws-192.168.1.9-8266")
        self.assertEqual(hold.spawn_name(["micropython", "-X", "heapsize=8M"]), "micropython")
        self.assertEqual(hold.spawn_name(["/x/bin/micropython.exe"]), "micropython-exe")


class ParseReplyTests(unittest.TestCase):
    def test_reply_between_echo_and_prompt(self):
        r = hold.parse_reply("x = 6*7; print(x)\n42\n>>> ", "x = 6*7; print(x)")
        self.assertTrue(r["prompt"])
        self.assertEqual(r["output"], "42\n")

    def test_app_output_stays_in_the_reply(self):
        text = "print('@@', 1)\ntick 5\n@@ 1\ntick 6\n>>> "
        r = hold.parse_reply(text, "print('@@', 1)")
        self.assertEqual(hold.marked(r["output"], "@@"), ["1"])
        self.assertIn("tick 5", r["output"])

    def test_a_prompt_from_before_the_echo_does_not_count(self):
        # The interpreter was busy with an earlier line; its prompt arrives
        # first, then our echo, then our answer.
        text = "done\n>>> print(2)\n2\n>>> "
        r = hold.parse_reply(text, "print(2)")
        self.assertEqual(r["output"], "2\n")

    def test_no_prompt_yet(self):
        r = hold.parse_reply("long_call()\n", "long_call()")
        self.assertFalse(r["prompt"])
        self.assertFalse(r["continuation"])

    def test_prompt_must_start_a_line(self):
        r = hold.parse_reply("print('>>> ')\n>>> \n", "print('>>> ')")
        # The printed '>>> ' starts a line too; the real prompt is the next one.
        self.assertTrue(r["prompt"])

    def test_continuation_with_auto_indent(self):
        r = hold.parse_reply("if True:\n...     ", "if True:")
        self.assertFalse(r["prompt"])
        self.assertTrue(r["continuation"])

    def test_paste_mode_reply_starts_after_the_last_paste_prompt(self):
        text = (
            "\npaste mode; Ctrl-C to cancel, Ctrl-D to finish\n=== a = 1\n"
            "=== print(a)\n=== \n1\n>>> "
        )
        r = hold.parse_reply(text, None, hold.PASTE_PROMPT)
        self.assertEqual(r["output"], "1\n")

    def test_raw_reply(self):
        self.assertIsNone(hold.parse_raw_reply("OKpartial"))
        r = hold.parse_raw_reply("OK42\r\n\x04\x04>")
        self.assertEqual(r, {"output": "42\r\n", "error": ""})

    def test_compound_one_liners_get_a_second_enter(self):
        self.assertTrue(hold._COMPOUND.match("for i in range(3): print(i)"))
        self.assertTrue(hold._COMPOUND.match("if x:"))
        self.assertFalse(hold._COMPOUND.match("print('a:b')"))

    def test_offset_of_skips_carriage_returns_and_counts_utf8(self):
        raw = "é\r\nab".encode("utf-8")
        self.assertEqual(hold._offset_of(len("é\nab"), raw), len(raw))
        self.assertEqual(hold._offset_of(1, raw), 2)


class VtTests(unittest.TestCase):
    def test_cursor_forward_is_spaces(self):
        # A full ConPTY screen writes the prompt's space as ESC[1C.
        self.assertEqual(hold.vt_to_text(b"\r\n>>>\x1b[1C"), (b"\r\n>>> ", b""))

    def test_other_escapes_go(self):
        text, carry = hold.vt_to_text(b"\x1b[?25l\x1b[2J\x1b[HMicro\x1b]0;title\x07Python")
        self.assertEqual((text, carry), (b"MicroPython", b""))

    def test_a_split_escape_waits_for_its_end(self):
        self.assertEqual(hold.vt_to_text(b"ab\x1b[1"), (b"ab", b"\x1b[1"))


class _HoldHome(unittest.TestCase):
    """A holder whose state lives in a temporary home."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        holds = Path(self.tmp.name) / ".mpftp" / "holds"
        env = {"HOME": self.tmp.name}
        self.patches = [
            mock.patch.dict(os.environ, env),
            mock.patch.object(hold, "HOLDS_DIR", holds),
        ]
        for p in self.patches:
            p.start()
        self.key = None

    def tearDown(self):
        if self.key:
            hold.stop(self.key)
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()


@unittest.skipIf(sys.platform == "win32", "POSIX pseudo terminal")
class CPythonReplTests(_HoldHome):
    def test_start_ask_read_stop(self):
        res = hold.start(
            spawn=f"{sys.executable} -q -i", name="cpy", env={"PYTHON_BASIC_REPL": "1"}
        )
        self.key = res["key"]
        self.assertTrue(res["prompt"], res)
        r = hold.ask(self.key, "print('@@', 6 * 7)", marker="@@")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["marked"], ["42"])
        # The same interpreter answers the next, separate call.
        hold.ask(self.key, "x = 5")
        self.assertEqual(hold.ask(self.key, "print('@@', x + 1)", marker="@@")["marked"], ["6"])
        # One holder per name.
        with self.assertRaises(hold.HoldError):
            hold.start(spawn=f"{sys.executable} -q -i", name="cpy")
        off = hold.write(self.key, b"print('later')\r")
        got = hold.read(self.key, since=off, wait=5, until="^later$")
        self.assertTrue(got["matched"], got)
        stopped = hold.stop(self.key)
        self.key = None
        self.assertTrue(stopped["was_running"])
        self.assertIsNone(hold.holder_pid("cpy"))

    def test_busy_says_no_prompt_yet_then_interrupt(self):
        res = hold.start(
            spawn=f"{sys.executable} -q -i", name="cpy2", env={"PYTHON_BASIC_REPL": "1"}
        )
        self.key = res["key"]
        r = hold.ask(self.key, "import time; time.sleep(30)", timeout=1.0)
        self.assertFalse(r["ok"])
        self.assertIn("no prompt yet", r["error"])
        i = hold.interrupt(self.key)
        self.assertTrue(i["ok"], i)
        self.assertIn("KeyboardInterrupt", i["output"])


@unittest.skipUnless(shutil.which("micropython") and sys.platform != "win32", "Linux MicroPython")
class MicroPythonReplTests(_HoldHome):
    def test_paste_and_exec_then_back_to_the_friendly_prompt(self):
        res = hold.start(spawn="micropython", name="mp")
        self.key = res["key"]
        self.assertTrue(res["prompt"], res)
        code = "def f(n):\n    return n * 2\n\nprint('@@', f(21))\n"
        self.assertEqual(hold.ask(self.key, code, marker="@@")["marked"], ["42"])
        r = hold.raw_exec(self.key, "print(f(4))\n1/0")
        self.assertFalse(r["ok"])
        self.assertEqual(r["output"].strip(), "8")
        self.assertIn("ZeroDivisionError", r["error"])
        # Back at the friendly prompt afterwards.
        self.assertEqual(hold.ask(self.key, "print('@@', f(1))", marker="@@")["marked"], ["2"])


if __name__ == "__main__":
    unittest.main()
