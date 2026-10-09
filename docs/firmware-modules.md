# Building firmware

`mpftp firmware build` builds with
[micropython-pydevices](https://github.com/PyDevices/micropython-pydevices)'
`build_mp.py`. You choose a port, a board or variant, and the modules to
compile in, and mpftp runs that one command for you, so a build from mpftp is
the same build you'd get typing `build_mp.py` yourself.

```bash
mpftp firmware list --port esp32 --board ESP32_GENERIC_P4   # what you can pick
mpftp firmware build --port esp32 --board ESP32_GENERIC_P4 --variant C6_WIFI \
    --flash 16MB --modules audiodsp,displayif,~/earful
```

The second line runs
`build_mp.py --port esp32 --board ESP32_GENERIC_P4 --variant C6_WIFI --modules=audiodsp,displayif,/home/you/earful --flash 16MB`,
streams its log, and prints where the firmware landed. `mpftp firmware flash`
with the same target flashes it.

## Getting the build system

If you don't have a micropython-pydevices checkout yet, this clones one into
`./micropython-pydevices`:

```bash
curl -fsSL https://pydevices.github.io/install.sh | sh
```

mpftp looks for the checkout in this order:

1. `--build-system PATH`
2. the `buildSystemPath` setting in `~/.mpftp/config.json`, or `MPFTP_BUILD_SYSTEM`
3. the folder you run mpftp from and each of its parents, as the checkout
   itself or as the folder holding `micropython-pydevices/`
4. beside a MicroPython checkout given with `--mp` (the checkout's own
   `micropython/`, or a sibling of it)
5. `~/micropython-pydevices`

`build_mp.py` fetches the rest on the first build: MicroPython at its pinned
tag, the modules at their pinned commits, and the toolchain the port needs
(ESP-IDF for esp32, emsdk for webassembly) at its locked version. mpftp still
checks for a cross-compiler that has to be on your PATH, such as
`arm-none-eabi-gcc` for rp2, and says what to install if it's missing.

## Walking a target from the terminal

`mpftp firmware list` goes one level at a time. On its own it lists the ports.
With `--port` it lists that port's boards and their variants, or the port's
variants. Once you've picked a board or variant, it lists the modules and
prints the build command. `--json` prints the whole tree for scripts.

Everything in these lists comes from `build_mp.py` itself, so a board or
variant it can build shows up here, ours and upstream's alike.

## Modules

`mpftp firmware modules` lists what `--modules` takes:

- a module's short name, from the checkout's `modules/` folder. Each is marked
  **C** or **Python**. A module pinned in `modules.lock` that hasn't been
  fetched yet is listed too, marked "fetched on first build".
- `all`, which is every module except the opt-in ones (marked **opt-in**,
  such as `tflite` and `vision`, which are large). Name an opt-in module to
  build it.
- a full path to a module anywhere else, such as `~/earful` or `./mymod`.
  mpftp makes a relative path absolute before it hands it on.

Leave `--modules` out and you get the target with none of ours compiled in.
A module never brings in another, so name what it needs too: the instruments
in `audiocomponents` import `ulab`, for example.

In the Firmware panel the same choice is the **Modules** card under Select,
one checkbox per module.

## Other build options

| Option | What it does |
|---|---|
| `--interpreter circuitpython` | Builds CircuitPython-compatible firmware instead: CircuitPython's own ports and boards (`raspberrypi`, `espressif`, `atmel-samd`, `unix`, ...) with our C modules compiled in. `mpftp firmware list --interpreter circuitpython` lists them once `build_mp.py` has fetched CircuitPython, which its first CircuitPython build does. |
| `--flash 16MB` | esp32 flash size. Upstream's generic boards assume 4 MB, which most module sets outgrow. Leave it out to keep the board's own. |
| `--no-autosize` | When the image doesn't fit, `build_mp.py` grows the app partition (esp32) or shrinks the filesystem (rp2) and builds again. This refuses instead and prints the layout that would fit. |
| `--clean` | Deletes this target's build folder first. |
| `--out-dir DIR` | Builds under `DIR` instead of the checkout's `builds/` (it is `build_mp.py`'s `OUT_DIR`). Pass the same `--out-dir` to `firmware flash` and `firmware artifact`. |
| `--jobs N` | Parallel jobs; the default is every core. |
| `--make-arg ARG` | An argument for `make`, such as `--make-arg CIRCUITPY_ULAB=0` to free flash on a small CircuitPython board. Repeat it for more. |

Builds land in `builds/<port>/[<board>/]<variant>/`, and CircuitPython ones in
`builds/circuitpython/<port>/<board or variant>/`. Each folder records what
went into it; how `build_mp.py` works is in its
[build plan](https://github.com/PyDevices/micropython-pydevices/blob/main/docs/build-plan.md).

## When a build fails

mpftp reports `build_mp.py`'s own reason as the error, such as
`no board 'ESP32_GENERIC_P5' for esp32 (have: ...)`. When `make` failed, the
result also carries the first compiler or linker error line as `detail`, so you
don't have to scroll the log for it. A missing choice names the flag to add:
`build_mp.py needs --board (see mpftp firmware list --port esp32)`.

## What changed from earlier versions

mpftp used to build from a MicroPython checkout with the modules found beside
it, and offered presets read from an overlay's `manifests/` folder. Those
folders are gone from micropython-pydevices, and so are presets: name the
modules, or `all`. `--board-dir`, `--variant-dir`, `--module-roots` and the
`firmwareModuleRoots` setting are gone too, and `--build-dir` became
`--out-dir`. The `esp32_partitions/` overrides that `firmware partitions`
writes aren't used by `build_mp.py`, which picks the partition table itself.
