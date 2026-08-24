---
description: Use when connecting to, deploying code to, or debugging a MicroPython or CircuitPython board (ESP32, RP2040, SAMD, etc.) over serial via the mpftp MCP server — filesystem transfer, REPL/exec, non-interrupting output capture, and firmware build/flash.
---

# mpftp board tools

Typed tools backed by a real serial session with a MicroPython/CircuitPython
board: `list_ports`, `connect`, `disconnect`, `fs_ls`/`fs_tree`/`fs_read`/
`fs_write`/`fs_cp`/`fs_rm`/`fs_mkdir`/`fs_hash`, `exec_code`/`eval_expr`,
`run_script`/`run_path`, `watch_repl`, `interrupt`/`soft_reset`/
`soft_reboot`/`hard_reset`, `probe`, and `firmware_discover`/`firmware_tree`/
`firmware_build`/`firmware_flash`.

## The one thing to internalize: raw REPL interrupts whatever's running

`exec_code`, `eval_expr`, `run_script`, `run_path`, `fs_write`, `fs_read`,
and every other filesystem/exec tool enter the board's **raw REPL**, which
sends Ctrl-C first — this stops any script currently running, including one
you just started. There is no way around this at the protocol level; it's
how raw REPL works. Two tools exist specifically because of this:

- **`watch_repl(duration)`** — tails the board's own stdout for `duration`
  seconds *without* entering raw REPL, so a running script keeps running.
  Have the script `print()` its own progress; call `watch_repl` again to
  keep watching. This is the only way to see progress from something you
  don't want to interrupt.
- **`probe(file, wait, capture, reboot_first)`** — runs a local script with
  `run_script`, sleeps `wait` seconds, then reads `capture` back — the
  pattern for "run this, let it work, then get the result," bundled into
  one call instead of three so you don't lose the "did it start OK" signal
  along the way. `reboot_first` hard-resets before running when stale
  module state (armed timers, already-imported config) would otherwise make
  a fine board look broken a few iterations in — pass `device` when you use
  it, since that's what it reconnects to after the reset.

If you just want a script's final output and don't care about interrupting
it, `run_script(follow=true)` blocks until it exits and returns its output
directly — simpler than `probe` when there's nothing to wait for.

## Soft reset vs soft reboot

- **`soft_reset`**: MicroPython raw soft-reset — reinitializes the
  interpreter but does **not** run `main.py`. CircuitPython: toggles
  friendly↔raw REPL, does not run `code.py`. Use after a bad import or a
  wedged module before you want a clean interpreter without restarting the
  app.
- **`soft_reboot`**: Ctrl-D — runs `main.py` / `code.py`, i.e. actually
  starts the deployed app. Use this after deploying, not `soft_reset`
  (deploying, then `soft_reset`, looks like "nothing happened" — the app
  never started).
- **`hard_reset`**: full reset. Use when the port is wedged or after
  something that needs a real power-on-equivalent reset.

## CircuitPython specifics

- `fs_write`/`fs_cp` write through the mounted **CIRCUITPY** USB drive when
  it's mounted (same as dragging files in Finder/Explorer); serial is the
  fallback when it isn't.
- Compile-on-upload (`mpy: true` on `fs_write`/`fs_cp`) is **MicroPython
  only** — it's a clear error on CircuitPython, not a silent `.py` fallback.
- Packages: CircuitPython uses `circup` (not exposed as an MCP tool here —
  use the mpftp CLI's `circup` command for library installs); MicroPython
  packages go through `mip`, similarly CLI-only for now.

## Firmware

`firmware_discover`/`firmware_tree` are quick and read-only. `firmware_build`
and `firmware_flash` can take minutes (a full build) and are destructive in
the sense that `firmware_flash` overwrites the board — confirm the target
device/artifact before calling, especially `firmware_flash` with `erase:
true`, which wipes the filesystem partition.

## Full reference

The mpftp CLI's `docs/agent-guide.md` (in the [mpftp
repo](https://github.com/PyDevices/mpftp)) has the complete playbook this
skill condenses, including a troubleshooting table for wedged ports, dual-USB
boards, and ESP32 partition-overflow autosize — worth reading directly if
you're working inside a checkout of mpftp itself rather than just using it as
a tool.
