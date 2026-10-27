"""The sidecar's children die with it on Windows (mpftp#146).

Run from WSL, the sidecar is ended when the Linux command that started it is
killed, but circup and the other children it started were left holding the
COM port. A job object with KILL_ON_JOB_CLOSE ends them with the sidecar.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
import unittest

from mpftp import sidecar


class JobTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "off Windows there is no job to set up")
    def test_off_windows_it_does_nothing(self):
        self.assertFalse(sidecar.kill_children_with_me())

    @unittest.skipUnless(sys.platform == "win32", "job objects are Windows only")
    def test_child_dies_when_the_sidecar_does(self):
        code = textwrap.dedent(
            """
            import os, subprocess, sys
            from mpftp import sidecar
            assert sidecar.kill_children_with_me()
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                     stdin=subprocess.DEVNULL)
            print(child.pid, flush=True)
            os._exit(0)  # no cleanup, like a process ended from outside
            """
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        pid = int(out.stdout.split()[-1])
        deadline = time.time() + 10
        alive = True
        while alive and time.time() < deadline:
            probe = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, timeout=30
            )
            alive = str(pid) in probe.stdout
            if alive:
                time.sleep(0.3)
        self.assertFalse(alive, "the sidecar's child outlived it")


if __name__ == "__main__":
    unittest.main()
