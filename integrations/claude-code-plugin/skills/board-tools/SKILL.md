---
description: Use when connecting to, deploying code to, or debugging a MicroPython or CircuitPython board (ESP32, RP2040, SAMD, etc.) with the mpftp CLI — file transfer, REPL/exec, driving a running app with `mpftp hold`, non-interrupting output capture, and firmware build/flash.
---

# mpftp board tools

You drive the board with the `mpftp` command (`pip install pydevices-mpftp`).
The full playbook is the [agent guide](https://github.com/PyDevices/mpftp/blob/main/docs/agent-guide.md):
read it before anything unusual, and use its troubleshooting table when a
port wedges. This page is the short version.

```bash
mpftp ports                      # what's attached
mpftp connect COM4               # or /dev/ttyACM0, ws://HOST, ble://NAME
mpftp status                     # who holds the session, which device
mpftp put main.py /main.py       # also get, cp, ls, tree, rm, mkdir, hash
mpftp exec "import gc; print(gc.mem_free())"
mpftp run app.py --follow        # run a local script, wait for its output
```

Pass `-d DEVICE` after the subcommand when no editor session is connected.

## Raw REPL interrupts whatever is running

`exec`, `eval`, `run`, `put`, `get` and the other file commands enter the
board's raw REPL, which sends Ctrl-C first. That stops any script running,
including one you just started. Three commands exist because of this:

- **`mpftp hold`** keeps one friendly REPL open across separate calls, so you
  can talk to an app while it runs: `hold start -d COM4`, then `hold ask
  "print(app.state)"`, `hold read`, `hold interrupt`, and `hold stop` to let
  go. Nothing it does resets the board or enters the raw REPL except `hold
  exec`. While a holder owns a board, other mpftp commands on it are refused.
  See "Drive a running app from its REPL" in the agent guide.
- **`mpftp watch-repl`** tails the board's own stdout without entering the raw
  REPL. Have the script `print()` its progress.
- **`mpftp probe FILE --wait N --capture PATH`** runs a script, waits, then
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
to `.mpy` on upload is MicroPython only. Libraries install with `mpftp
circup`; MicroPython packages with `mpftp mip`.

## Firmware

`mpftp firmware detect`, `discover`, `list` and `modules` are quick and
read-only.
`firmware build` can take minutes, and `firmware flash` overwrites the board:
confirm the device and the image first, especially with `--erase`, which
erases the whole flash, filesystem included.
