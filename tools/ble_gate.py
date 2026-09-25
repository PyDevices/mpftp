#!/usr/bin/env python3
"""The ble:// gate, in one sidecar session, as the VS Code extension drives it.

Against a board running ``bledev.filetransfer.start()`` (or
``bledev.repl.start()``) from main.py, with no serial port involved:

1. connect and log in; list ``/``
2. put 20 KB, get it back, byte for byte, and the board's SHA-256 agrees
3. exec prints what it should
4. a running ``while True`` loop is interrupted, and the REPL still answers

On WSL, run it with the Windows Python, which has bleak and the radio:

    PYTHONPATH=cli/src MPFTP_BLE_PASSWORD=... WSLENV=MPFTP_BLE_PASSWORD:PYTHONPATH/p \\
        python.exe "$(wslpath -w tools/ble_gate.py)" ble://bledev-files

The planted faults are the CLI's (``MPFTP_BLE_PLANT=put mpftp put ...`` must
fail its hash check); see docs/plans/ble.md. Ends with ``RESULT PASS`` or
``RESULT FAIL``.
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
import time

from mpftp import sidecar

failures: list[str] = []


def check(ok: bool, what: str, detail: str = "") -> None:
    print("PASS" if ok else "FAIL", what, detail)
    if not ok:
        failures.append(what)


def main() -> None:
    device = sys.argv[1] if len(sys.argv) > 1 else "ble://bledev-files"
    s = sidecar.Session()
    t0 = time.monotonic()
    res = s.connect(device, password=os.environ.get("MPFTP_BLE_PASSWORD"))
    check(res.get("interpreter") == "micropython", "connect and log in", f"{time.monotonic() - t0:.1f} s")

    names = [e.get("name") for e in s.fs_listdir("/")]
    check("lib" in names, "ls /", repr(names))

    data = os.urandom(20 * 1024)
    path = "/ble_gate.bin"
    a = time.monotonic()
    put = s.fs_write(path, base64.b64encode(data).decode("ascii"), verify=True)
    b = time.monotonic()
    back = base64.b64decode(s.fs_read(path)["data_b64"])
    c = time.monotonic()
    board = s.fs_hash(path)["hash"]
    check(put.get("verified") == hashlib.sha256(data).hexdigest(), "put 20 KB, verified", f"{b - a:.2f} s")
    check(back == data, "get 20 KB byte for byte", f"{c - b:.2f} s")
    check(board == hashlib.sha256(data).hexdigest(), "the board's SHA-256 matches")
    s.fs_rm(path)

    out = s.exec("print(6 * 7)")["output"]
    check(out.strip() == "42", "exec", repr(out))

    s.run_script("n = 0\nwhile True:\n    n += 1\n", follow=False)
    time.sleep(2.0)
    busy = s.exec("print('ran', n > 1000)")["output"]  # connect-style: Ctrl-C first
    check("ran True" in busy, "a later command interrupts the loop first", repr(busy))

    s.run_script("n = 0\nwhile True:\n    n += 1\n", follow=False)
    time.sleep(2.0)
    r = s.interrupt()
    check(bool(r.get("ok")), "interrupt stops a running loop", repr(r))
    out = s.exec("print('after', n > 1000)")["output"]
    check("after True" in out, "the REPL answers after the interrupt", repr(out))

    s.disconnect()


try:
    main()
except Exception as e:
    failures.append("exception")
    print("EXCEPTION", type(e).__name__, e)
print("RESULT", f"FAIL {failures}" if failures else "PASS")
sys.exit(1 if failures else 0)
