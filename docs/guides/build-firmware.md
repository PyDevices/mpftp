# Build firmware

`mpftp firmware build` builds with
[micropython-pydevices](https://github.com/PyDevices/micropython-pydevices)'
`build_mp.py`. You choose a port, a board or variant, and the modules to
compile in, and mpftp runs that one command for you, so a build from mpftp is
the same build you get by typing `build_mp.py` yourself.

```bash
mpftp firmware list --port esp32 --board ESP32_GENERIC_P4   # what you can pick
mpftp firmware build --port esp32 --board ESP32_GENERIC_P4 --variant C6_WIFI \
    --flash 16MB --modules audiodsp,displayif,~/earful
```

The second line runs
`build_mp.py --port esp32 --board ESP32_GENERIC_P4 --variant C6_WIFI --modules=audiodsp,displayif,/home/you/earful --flash 16MB`,
streams its log, and prints one JSON result saying where the firmware landed.
`mpftp firmware flash` with the same target flashes it
([Download and flash firmware](download-and-flash-firmware.md)). In the editor,
the Firmware panel's **Build** mode does the same with a Target card for ports,
boards and variants, and a Modules card with one checkbox per module.

## Get the build system

If you don't have a micropython-pydevices checkout, this clones one into
`./micropython-pydevices`:

```bash
curl -fsSL https://pydevices.github.io/install.sh | sh
```

mpftp finds the checkout by the order in
[Finding the tools you need](../how-it-works/finding-tools.md#the-build-system).
To name it outright, pass `--build-system PATH` or set `mpftp.buildSystemPath`:
that wins over everything else, and a folder that isn't a checkout is an error,
not a fallback. `build_mp.py` fetches the rest on the first build: MicroPython,
the modules and the toolchain the port needs.

## Walk a target from the terminal

`mpftp firmware list` goes one level at a time. On its own it lists the ports.
With `--port` it lists that port's boards and variants. Once you have picked a
board or variant, it lists the modules and prints the build command. `--json`
prints the whole tree. Everything comes from `build_mp.py`, so a board it can
build shows up, yours and upstream's alike.

## Choose modules

`mpftp firmware modules` lists what `--modules` takes:

- A module's short name, from the checkout's `modules/` folder, marked **C**
  or **Python**. A module pinned in `modules.lock` but not fetched yet is
  listed too, marked "fetched on first build".
- `all`, which is every module except the opt-in ones (such as `tflite` and
  `vision`, which are large). Name an opt-in module to build it.
- A full path to a module anywhere else, such as `~/earful` or `./mymod`. mpftp
  makes a relative path absolute before it hands it on.

Leave `--modules` out and you get the target with none of ours compiled in. A
module never brings in another, so name what it needs too: the instruments in
`audiocomponents` import `ulab`, for example.

## Build CircuitPython-compatible firmware

`--interpreter circuitpython` builds CircuitPython's own ports and boards
(`raspberrypi`, `espressif`, `atmel-samd`, `unix`) with the C modules compiled
in. `mpftp firmware list --interpreter circuitpython` lists them once
`build_mp.py` has fetched CircuitPython, which its first CircuitPython build
does. Flash a UF2 with `mpftp firmware flash --uf2 --artifact PATH`. The other
options are in [Firmware build options](../reference/firmware-build-options.md).

## When the image doesn't fit

When an esp32 image is larger than its app partition, `build_mp.py` grows the
partition, moves the ones after it, builds once more and prints the new layout.
rp2 gets the same by shrinking the filesystem. Pass `--no-autosize` to refuse
instead and print the layout that would fit. The filesystem moves either way, so
a board flashed with the new layout comes up with an empty one.

## When a build fails

mpftp reports `build_mp.py`'s own reason as the error, such as
`no board 'ESP32_GENERIC_P5' for esp32 (have: ...)`. When `make` failed, the
result also carries the first compiler or linker error as `detail`, so you
don't have to scroll the log. A missing choice names the flag to add. A missing
cross-compiler that has to be on your `PATH` (`arm-none-eabi-gcc` for rp2) is
named too: install it, or use **Locate…** in the Firmware panel for its `bin/`
folder.

`build_mp.py` builds one MicroPython target at a time per checkout and waits
for another build to finish. To build two targets side by side, give each its
own `--out-dir`, and pass the same `--out-dir` to `flash` and `artifact`.
