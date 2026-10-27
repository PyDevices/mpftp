# Firmware build options

The flags of `mpftp firmware build`. How to use them is in
[Build firmware](../guides/build-firmware.md).

| Option | What it does |
|---|---|
| `--port`, `--board`, `--variant` | The target. `mpftp firmware list` shows what `build_mp.py` offers |
| `--modules a,b,PATH` | Modules to compile in: names from `firmware modules`, full paths, or `all`. Default is none |
| `--interpreter circuitpython` | Builds CircuitPython-compatible firmware instead: CircuitPython's own ports and boards (`raspberrypi`, `espressif`, `atmel-samd`, `unix`) with the C modules compiled in |
| `--flash 16MB` | esp32 flash size. Upstream's generic boards assume 4 MB, which most module sets outgrow. Leave it out to keep the board's own |
| `--no-autosize` | When the image doesn't fit, `build_mp.py` grows the app partition (esp32) or shrinks the filesystem (rp2) and builds again. This refuses instead and prints the layout that would fit |
| `--clean` | Deletes this target's build folder first |
| `--out-dir DIR` | Builds under `DIR` instead of the checkout's `builds/` (`build_mp.py`'s `OUT_DIR`). Pass the same `--out-dir` to `firmware flash` and `firmware artifact` |
| `--jobs N` | Parallel jobs. The default is every core |
| `--make-arg ARG` | An argument for `make`, such as `--make-arg CIRCUITPY_ULAB=0` to free flash on a small CircuitPython board. Repeat it for more |
| `--build-system PATH` | The micropython-pydevices checkout. See [Finding the tools you need](../how-it-works/finding-tools.md#the-build-system) |
| `--mp PATH` | The MicroPython tree, if it isn't found on its own |

Builds land in `builds/<port>/[<board>/]<variant>/`, and CircuitPython ones in
`builds/circuitpython/<port>/<board or variant>/`. Each folder records what
went into it. `build_mp.py`'s options are in its
[reference](https://github.com/PyDevices/micropython-pydevices/blob/main/docs/reference/build-command.md).

## The other firmware commands

| Command | What it does |
|---|---|
| `firmware list` | The ports, then a port's boards and variants, then the modules; `--json` for the whole tree |
| `firmware modules` | The modules `--modules` takes, marked **C**, **Python** or **opt-in** |
| `firmware discover` | The resolved MicroPython, ESP-IDF, emsdk and build-system paths |
| `firmware build` | Build, streaming `build_mp.py`'s log, and print one JSON result |
| `firmware artifact` | Report the firmware built for a selection |
| `firmware clean` | Delete a selection's build folder |
| `firmware detect` | An esptool-first chip, flash and security probe |
| `firmware flash` | Flash a built or downloaded artifact (`--artifact`, `--offset`, `--erase`, `--uf2`, `--uf2-timeout`) |
| `firmware download-tree`, `download-list`, `download` | The official firmware catalog |
| `firmware ptable IMAGE` | Print an image's partition table; `--compare` diffs two, `--device` diffs a board's |
| `firmware partitions` | esp32 partition override (`get`, `set`, `reset`, `candidates`, `split`). `build_mp.py` picks the partition table itself and doesn't use these overrides |
