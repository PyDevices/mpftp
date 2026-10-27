# Run code and use the REPL

## The REPL

**mpftp: Open REPL** opens a terminal on the same session as File Transfer,
with ANSI colors, in the editor's Terminal panel. Interrupt, soft reset, soft
reboot and hard reset are commands too. After a hung `exec` or `run`, mpftp
releases the dead port handle (`transport_dead` in the activity log) so
Connect or Resume can take the port back.

## Run something once

```bash
mpftp eval '1+1'
mpftp exec 'print(1)'                # waits for the program to end
mpftp run ./app.py                   # starts it and returns
mpftp run ./short.py --follow        # waits for it to finish
```

`exec` waits for the program's output to end, so use `--no-follow` for a
loop. `run` returns immediately by default, so an editor session stays usable.
If a command says `timeout waiting for first EOF`, the board is still running
your program: use `run` without `--follow`, then `interrupt` or `soft-reset`.
When a `run --follow` times out, the error carries `partialOutput`, everything
printed up to that point.

## Interrupt and reset

| Command | MicroPython | CircuitPython |
|---|---|---|
| `interrupt` | Ctrl-C, no reset | the same |
| `soft-reset` | Raw soft reset: skips `main.py` | Toggles between the friendly and raw REPL: does not run `code.py` |
| `soft-reboot` | Ctrl-D: runs `main.py` | Ctrl-D: runs `code.py` |
| `hard-reset` | The board boots normally | the same |

After a deploy, a board that looks dead after `soft-reset` is expected: it
skipped `main.py`. Use `soft-reboot` or `hard-reset` to run startup. Debug
with `exec`, `eval` or `run` before rewriting `main.py`.

## Watch a program without stopping it

`get`, `put` and `exec` all enter the raw REPL, which interrupts whatever is
running. To follow a long script without killing it:

1. `mpftp run --no-follow script.py` starts it and returns.
2. `mpftp watch-repl` subscribes to the board's output and never sends
   Ctrl-C. Ctrl-C on the host stops watching; the board keeps running.

Have the script `print()` its own progress. To talk to the program as well as
watch it, [hold its REPL](drive-a-running-app.md).

If you need a file's final contents, have the script write it and `get` it
when the script is done. `probe` bundles that cycle:

```bash
mpftp probe -d /dev/ttyACM0 probe.py --reboot-first --capture /result.txt --wait 20
```

`--reboot-first` hard-resets and reconnects before the run, since stale module
state from a previous try can make a working board look broken. It needs
`--device`. `--wait` is a flat delay before the capture read, and `--capture`
is optional. The result is JSON either way, and a failed capture sets
`"ok": false` and `capture_error` instead of losing the fact that the run
itself worked.

## Plot what a board prints

In the [browser interface](use-the-browser-interface.md), the Plotter button
graphs lines that are only numbers.
