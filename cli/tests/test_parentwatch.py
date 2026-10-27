"""The desktop helpers end with the app that started them (mpftp#159)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

# Starts the module given in argv[1] as a child that shares this process's
# stdin, prints the child's pid, then waits to be killed.
MIDDLE = (
    "import subprocess, sys, time\n"
    "p = subprocess.Popen([sys.executable, '-m', sys.argv[1]],\n"
    "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "print(p.pid, flush=True)\n"
    "time.sleep(120)\n"
)


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        h = k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
        k32.CloseHandle(ctypes.c_void_p(h))
        return code.value == 259  # STILL_ACTIVE
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False
    except OSError:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


class ParentWatchTests(unittest.TestCase):
    def _run(self, module: str) -> None:
        env = dict(os.environ, PYTHONPATH=str(SRC), MPFTP_EXIT_WITH_PARENT="1")
        env.pop("MPFTP_PYTHON", None)
        # This test holds the middle process's stdin open, so the child never
        # sees end-of-file: only watching its parent can end it.
        mid = subprocess.Popen(
            [sys.executable, "-c", MIDDLE, module],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=env,
            text=True,
        )
        child = 0
        try:
            child = int(mid.stdout.readline())
            time.sleep(2)  # let the child start and arm its watch
            self.assertTrue(_alive(child), f"{module} died before the parent was killed")
            mid.kill()
            mid.wait(timeout=10)
            deadline = time.time() + 15
            while time.time() < deadline and _alive(child):
                time.sleep(0.25)
            self.assertFalse(_alive(child), f"{module} outlived its parent")
        finally:
            if child and _alive(child):
                try:
                    os.kill(child, 9)
                except OSError:
                    pass
            mid.kill()
            mid.wait(timeout=10)
            for f in (mid.stdin, mid.stdout):
                if f:
                    f.close()

    def test_sidecar_ends_with_its_parent(self) -> None:
        self._run("mpftp.sidecar")

    def test_desktop_host_ends_with_its_parent(self) -> None:
        self._run("mpftp.desktop_host")

    def test_off_unless_asked(self) -> None:
        from mpftp.parentwatch import exit_with_parent

        os.environ.pop("MPFTP_EXIT_WITH_PARENT", None)
        self.assertFalse(exit_with_parent())


if __name__ == "__main__":
    unittest.main()
