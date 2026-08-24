"""Run an mpftp entry point from the copy vendored inside the VSIX.

The extension spawns Python by absolute *script path* so that platform.ts can
translate a WSL path into a Windows one for python.exe. Two things that would
otherwise be simpler do not survive that boundary:

  * ``python -m mpftp.sidecar`` needs the package on sys.path, and PYTHONPATH
    only crosses the WSL/Windows interop boundary when it is listed in WSLENV.
  * running a module file inside the package directly leaves __package__ unset,
    so its relative imports fail.

A launcher sitting next to the vendored package avoids both: it is an ordinary
script path, and it puts its own directory on sys.path before importing.

Usage: mpftp_launch.py {sidecar|firmware|cli} [args...]
"""

import os
import sys

TARGETS = ("sidecar", "firmware", "cli")


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in TARGETS:
        sys.stderr.write(f"usage: {os.path.basename(__file__)} {{{'|'.join(TARGETS)}}} [args...]\n")
        return 2

    target = sys.argv.pop(1)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    module = __import__(f"mpftp.{target}", fromlist=["main"])
    return module.main() or 0


if __name__ == "__main__":
    sys.exit(main())
