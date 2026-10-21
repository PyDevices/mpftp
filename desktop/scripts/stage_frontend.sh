#!/usr/bin/env bash
# Stage the desktop app's pages into .stage/web: mpftp's web UI, the
# emulator window, and the WebAssembly runtime the emulator runs.
#
# The runtime is the PyDevices MicroPython build, with the PyDevices
# libraries frozen in. Point MPFTP_EMU_RUNTIME at a folder holding
# micropython.mjs and micropython.wasm to use a different one.
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
repo="$(cd "$here/.." && pwd)"
runtime="${MPFTP_EMU_RUNTIME:-$repo/../../../workbench/assets/pydevices}"
out="$here/.stage/web"

for f in micropython.mjs micropython.wasm; do
  [[ -f "$runtime/$f" ]] || { echo "stage_frontend: no $f in $runtime" >&2; exit 1; }
done

rm -rf "$out"
mkdir -p "$out/emulator/runtime"
cp -r "$repo/cli/src/mpftp/webui/." "$out/"
cp "$here/emulator/emulator.html" "$here/emulator/emulator.js" "$here/emulator/emu-core.mjs" "$out/emulator/"
cp "$runtime/micropython.mjs" "$runtime/micropython.wasm" "$out/emulator/runtime/"
echo "staged $(du -sh "$out" | cut -f1) in $out (runtime from $runtime)"
