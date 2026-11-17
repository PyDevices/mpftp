#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brad Barnett
#
# SPDX-License-Identifier: MIT
"""Check that a built mcuctl wheel carries its types to a user.

From the repository root, with `build` and `mypy` installed:

    python tools/check_typed.py

mcuctl annotates its code, so it ships no stubs: the wheel carries a `py.typed`
marker (PEP 561) and a type checker reads the annotations from the installed copy.
This builds the wheel, installs it without its dependencies into a scratch folder,
and runs mypy over two samples there. `tools/typed/sample_ok.py` must pass, and
`tools/typed/sample_bad.py` must fail with its one planted error, so the marker is
proved to reach a user and the annotations to catch a mistake.
"""

import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "tools" / "typed"
TIMEOUT = 300  # seconds, per step


def run(command, env=None):
    try:
        return subprocess.run(command, env=env, timeout=TIMEOUT, capture_output=True, text=True, check=False)
    except subprocess.TimeoutExpired:
        print("timed out: " + " ".join(command))
        return None


def show(result):
    if result is not None:
        print((result.stdout + result.stderr).rstrip())


def main():
    status = 0
    with tempfile.TemporaryDirectory() as scratch:
        scratch = Path(scratch)
        print("== build the wheel")
        result = run([sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(scratch / "dist"), str(ROOT)])
        if result is None or result.returncode != 0:
            show(result)
            return 1
        wheel = next((scratch / "dist").glob("mcuctl-*.whl"))
        with zipfile.ZipFile(wheel) as archive:
            print("py.typed in the wheel:", "mcuctl/py.typed" in archive.namelist())
            if "mcuctl/py.typed" not in archive.namelist():
                return 1

        site = scratch / "site"
        result = run([sys.executable, "-m", "pip", "install", "--quiet", "--no-deps", "--target", str(site), str(wheel)])
        if result is None or result.returncode != 0:
            show(result)
            return 1

        # Samples go in the scratch folder, so the checkout's own mcuctl can't answer
        # the import: only the installed copy can.
        # Not MYPYPATH, and no --ignore-missing-imports: either would let mypy read the
        # package whether or not it carries the marker.
        env = dict(os.environ, PYTHONPATH=str(site))
        mypy = [sys.executable, "-m", "mypy", "--no-incremental"]
        print("== sample a user writes: must pass")
        result = run(mypy + [str(SAMPLES / "sample_ok.py")], env)
        show(result)
        status |= result is None or result.returncode != 0

        print("== sample with one planted error: must fail")
        result = run(mypy + [str(SAMPLES / "sample_bad.py")], env)
        show(result)
        if result is None or result.returncode == 0 or "bundle_manifest_url" not in result.stdout:
            print("the planted error was not reported: the annotations are not being read")
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
