# Choosing what a firmware build carries

You pick the modules you want; mpftp compiles them in. A preset is only a
selection someone saved, so you can start from one and add to it.

```bash
mpftp firmware modules                      # what you can pick
mpftp firmware build --port esp32 --board WAVESHARE_ESP32_P4_PANEL \
    --variant PRE_REV3_C6_WIFI --preset kitchen-sink --modules earful
```

In the Firmware panel the same choice is the **Modules** card under Select:
a Preset menu and a checkbox per module. With no preset and nothing ticked,
the build is the target's own default, exactly what `make` would give you.

## Walking a target from the terminal

`mpftp firmware list` goes one level at a time. On its own it lists ports;
with `--port` it lists that port's boards (or variants); with a board or
variant it lists the modules and presets and prints the build command.
`--json` prints the whole tree for scripts.

## What counts as a module

mpftp looks in the folder that holds your `micropython/` checkout, in every
folder named by the `firmwareModuleRoots` setting (in `~/.mpftp/config.json`,
or `MPFTP_FIRMWARE_MODULE_ROOTS`), and in any `--module-roots` you pass. Each
child folder counts when:

- its `manifest.py` names a C half with `c_module(".")` (MicroPython 1.29),
- or it has a `micropython.mk` / `micropython.cmake` at its root (an older
  usermod; mpftp adds the `c_module()` line for it),
- or its `manifest.py` only freezes Python (`module()`, `package()`, ...),
  which the lists mark **freeze-only**.

A folder that looks CircuitPython-only (it has `apply_cp_patches.sh` and no
MicroPython build files) is skipped, and so is `pydevices`, which boards
install with mip rather than freeze. `--modules` also takes a path, for a
module outside every root.

## Dependencies

A module says what it needs by `include()`-ing the other module's manifest
from its own, for example `include("../audiodsp/manifest.py")`. mpftp
reads those lines to show "needs audiodsp", but it doesn't need them to
build: MicroPython visits each manifest once and compiles each C module
once, so two modules that share a dependency are safe to tick together.

## Presets, boards and variants from an overlay

An overlay is a folder with `manifests/*.py` beside `boards/` or `variants/`;
[micropython-pydevices](https://github.com/PyDevices/micropython-pydevices)
is one. Its manifests are listed as presets, and its boards and variants
appear beside upstream's, marked with the overlay's name. mpftp builds them
with `BOARD_DIR=` or `VARIANT_DIR=`, so you select them by name like any other
board. `--board-dir` and `--variant-dir` point at one that is not in an
overlay.

## What the build is given

The selection is written to `~/.mpftp/firmware/manifest-<target>.py` and
passed as `FROZEN_MANIFEST`. The file opens with the preset, or, without one,
with upstream's own manifest for the port (never an overlay board's default,
which is itself a preset), and then includes each ticked module's
`manifest.py`. Read it to see exactly what went into a build.

`--build-dir` builds somewhere other than the port's `build-<target>`
folder, which helps when several people share one checkout.

The older sibling-folder aggregators (`micropython.cmake` and
`manifest-micropython.py` at the workspace root) are no longer read.
