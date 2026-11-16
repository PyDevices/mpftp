---
description: Use when connecting to, deploying code to, or debugging a MicroPython or CircuitPython board (ESP32, RP2040, SAMD, etc.) with the mcuctl CLI — file transfer, REPL/exec, driving a running app with `mcuctl hold`, non-interrupting output capture, and firmware build/flash.
---

# mcuctl board tools

You drive the board with the `mcuctl` command (`pip install mcuctl`).
The full playbook is the [agent guide](https://github.com/PyDevices/mcuctl/blob/main/docs/guides/drive-a-board-from-an-agent.md):
read it before anything unusual, and use its troubleshooting table when a
port wedges. This page is the short version.

```bash
mcuctl ports                      # what's attached
mcuctl connect COM4               # or /dev/ttyACM0, ws://HOST, ble://NAME
mcuctl status                     # who holds the session, which device
mcuctl put main.py /main.py       # also get, cp, ls, tree, rm, mkdir, hash
mcuctl exec "import gc; print(gc.mem_free())"
mcuctl run app.py --follow        # run a local script, wait for its output
mcuctl run app.py --follow --timeout 0   # ...however long it stays quiet (default 10 s)
```

Pass `-d DEVICE` after the subcommand when no editor session is connected.

## Raw REPL interrupts whatever is running

`exec`, `eval`, `run`, `put`, `get` and the other file commands enter the
board's raw REPL, which sends Ctrl-C first. That stops any script running,
including one you just started. Three commands exist because of this:

- **`mcuctl hold`** keeps one friendly REPL open across separate calls, so you
  can talk to an app while it runs: `hold start -d COM4`, then `hold ask
  "print(app.state)"`, `hold read`, `hold interrupt`, and `hold stop` to let
  go. Nothing it does resets the board or enters the raw REPL except `hold
  exec`. While a holder owns a board, other mcuctl commands on it are refused.
  See [Drive a running app](https://github.com/PyDevices/mcuctl/blob/main/docs/guides/drive-a-running-app.md).
- **`mcuctl watch-repl`** tails the board's own stdout without entering the raw
  REPL. Have the script `print()` its progress.
- **`mcuctl probe FILE --wait N --capture PATH`** runs a script, waits, then
  reads a result file back, in one command. `--reboot-first` hard-resets
  before running when stale module state (armed timers, an already-imported
  config) would make a fine board look broken; it needs `-d`.

## Soft reset, soft reboot, hard reset

- **`soft-reset`** reinitializes the interpreter but does **not** run
  `main.py` (on CircuitPython it does not run `code.py`). Deploying and then
  soft-resetting looks like nothing happened.
- **`soft-reboot`** is Ctrl-D: it runs `main.py` / `code.py`. Use it after
  deploying.
- **`hard-reset`** is a full reset; the app runs afterwards. Add
  `--monitor SECONDS` to read the boot output back without the REPL, which is
  how you catch an app that fails at startup.

## CircuitPython

File commands write through the mounted CIRCUITPY drive when it's there, so
they don't interrupt the running program; serial is the fallback. Compiling
to `.mpy` on upload is MicroPython only. Libraries install with `mcuctl
circup`; MicroPython packages with `mcuctl mip`, and OmniPython packages with
`mcuctl omip`. When you don't know a package's name, `mcuctl search WORDS` looks
through micropython-lib, the OmniPython index and the
CircuitPython bundles and gives each hit's install line.

## Firmware

`mcuctl firmware detect`, `discover`, `list` and `modules` are quick and
read-only.
`firmware build` can take minutes, and `firmware flash` overwrites the board:
confirm the device and the image first, especially with `--erase`, which
erases the whole flash, filesystem included.
