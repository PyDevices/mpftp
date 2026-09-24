#!/usr/bin/env python3
"""Time file transfers to a board over BLE: file transfer against raw REPL.

Opens one ``ble://`` transport (the sidecar's own code path), then times a
20 KB upload and download, byte for byte checked, once over the board's
file-transfer service and once over the raw REPL (mpremote's
``fs_writefile``/``fs_readfile``, raw-paste). Connecting is timed apart.
Needs bleak, so on WSL run it with the Windows Python:

    PYTHONPATH="$(wslpath -w cli/src)" MPFTP_BLE_PASSWORD=... \\
        python.exe "$(wslpath -w tools/ble_bench.py)" ble://bledev-files --runs 3

Prints one line per transfer and a summary. The numbers in docs/plans/ble.md
came from this script.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import statistics
import sys
import time

from mpftp import ble


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("device", help="ble://NAME")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--size", type=int, default=20 * 1024)
    ap.add_argument("--no-throughput", action="store_true", help="keep Windows' default connection parameters")
    ap.add_argument("--only", choices=("files", "repl"), help="time one path only")
    ap.add_argument("--repl-chunk", type=int, default=256, help="mpremote's chunk size for the REPL path (its default: 256)")
    ns = ap.parse_args()

    password = os.environ.get("MPFTP_BLE_PASSWORD")
    t0 = time.monotonic()
    link = ble.BleSerial(ns.device, password, throughput=not ns.no_throughput)
    link.open()
    t = ble._transport_class()(link)
    t.enter_raw_repl(soft_reset=False)
    print(f"connected and logged in in {time.monotonic() - t0:.1f} s; mtu {link.mtu}; files served: {link.has_files}")

    data = os.urandom(ns.size)
    path = "/ble_bench.bin"
    results: dict[str, dict[str, list[float]]] = {}
    paths = [p for p in ("files", "repl") if ns.only in (None, p)]
    ok = True
    for run in range(ns.runs):
        for how in paths:
            os.environ[ble.FILES_ENV] = "repl" if how == "repl" else ""
            chunk = ns.repl_chunk
            a = time.monotonic()
            t.fs_writefile(path, data, chunk_size=chunk)
            b = time.monotonic()
            back = bytes(t.fs_readfile(path, chunk_size=chunk))
            c = time.monotonic()
            board = t.fs_hashfile(path, "sha256")
            board = board.hex() if isinstance(board, bytes) else str(board)
            good = back == data and board == hashlib.sha256(data).hexdigest()
            ok = ok and good
            up, down = b - a, c - b
            results.setdefault(how, {"up": [], "down": []})
            results[how]["up"].append(up)
            results[how]["down"].append(down)
            print(
                f"run {run + 1} {how:5}: up {up:6.2f} s ({ns.size / up / 1024:5.1f} KB/s), "
                f"down {down:6.2f} s ({ns.size / down / 1024:5.1f} KB/s), "
                f"{'byte for byte' if good else 'MISMATCH'}"
            )
    t.exec(f"import os\nos.remove({path!r})")
    t.exit_raw_repl()
    link.close()
    print("median of", ns.runs)
    for how, r in results.items():
        up, down = statistics.median(r["up"]), statistics.median(r["down"])
        print(f"  {how:5}: up {up:.2f} s ({ns.size / up / 1024:.1f} KB/s), down {down:.2f} s ({ns.size / down / 1024:.1f} KB/s)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
