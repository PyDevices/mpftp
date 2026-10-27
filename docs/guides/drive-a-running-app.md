# Drive a running app

`mpftp hold` lets you talk to an app while it runs, across as many separate
commands as you like. You can click a button by calling its handler, read
state back the moment it changes, or play a game by reading its variables
instead of looking at the screen. The same commands work on a board and on a
desktop interpreter.

The app has to give the prompt back. An app whose script sets up its UI and
returns keeps running on its timers while the REPL sits at `>>>`. Start it from
the REPL with `import`, so its objects stay reachable as `module.thing`.
CircuitPython ends the display when `code.py` returns, so this is MicroPython
(and CPython) only.

```bash
mpftp hold start -d PORT                     # a board: serial, or -d ws://HOST
mpftp hold ask "import sys; sys.path.append('/app'); import calc" --timeout 30
mpftp hold ask "print('@@', calc.label.get_text())" --marker @@ --text
mpftp hold ask --file helpers.py             # several lines go in paste mode
mpftp hold read --wait 5 --until '^@@ done'  # what the app printed since the last read
mpftp hold interrupt                         # Ctrl-C, then waits for the prompt
mpftp hold stop                              # lets go; the board keeps running
```

## A desktop interpreter

`--spawn` starts a desktop interpreter on a pseudo terminal. Give MicroPython
its module path explicitly:

```bash
mpftp hold start --spawn "micropython -X heapsize=8M" --env MICROPYPATH=$MP --name linux-mp
mpftp hold start --spawn "micropython.exe -X heapsize=64M" --env MICROPYPATH=$MP
```

`micropython.exe` runs inside a Windows pseudo console (ConPTY, through the
Windows Python mpftp already uses for `COM` ports), because it reads keys with
the console API and spins on a plain pipe. mpftp turns `MICROPYPATH` into a
`;` list of Windows paths for it and resolves WSL symlinks the Windows side
can't open. Pass `MICROPYPATH` even if it is in your environment: without it,
the Windows build silently loads `%USERPROFILE%\.micropython\lib`.

With one holder running, the other `hold` commands find it. With several, name
one with `-d` (the device, or the `--name`).

## Commands

See [hold in the CLI reference](../reference/cli.md#mpftp-hold) for the table.
Every answer is JSON with a `next` offset, and `--text` prints only the answer.
Everything the device printed is in `~/.mpftp/holds/<key>/stream.bin`, and a
timestamped transcript of both directions is beside it in `transcript.log`.

## Tips

**Print answers behind a marker.** The app's own prints share the stream with
your answers. `print('@@', state)` and `--marker @@` picks yours out. `ask`
cuts its reply from just after the echo of what you typed to the next prompt,
so app output before it, and a prompt left from an earlier command, don't
confuse it.

**Click by calling the handler.** In LVGL, find the button and fire its event:
`obj.send_event(lv.EVENT.CLICKED, None)`. To find a button by its label, walk
`lv.screen_active()`'s children and cast labels with `lv.label.__cast__(child)`
after `child.check_type(lv.label_class)`. A non-LVGL appdev app keeps its
subscribers in `app._event_callbacks`; call them with a real
`events.Button(events.MOUSEBUTTONUP, (x, y), 1, True, None)` and the app can't
tell it from a tap. Load helpers like these once with `ask --file`.

**A busy interpreter doesn't answer.** Work run through `lv.async_call`, a long
call or a loop that doesn't yield holds the REPL. `ask` waits for `--timeout`
(10 s by default), then says "no prompt yet" with whatever came back, rather
than hanging. `read --wait` picks up from there, and `interrupt` stops it. A
line like `if x:` leaves the REPL waiting for the rest of the block; `ask` says
that after half a second. One-line blocks (`for i in r: f(i)`) get the empty
line that ends them.

**One holder per board.** While a holder has the board, other mpftp commands on
that device are refused with a pointer to `hold`, because the port is busy.
Stop it with `mpftp hold stop`, never `pkill -f`.

## Limits

- `send_event` runs a widget's handler the way a tap does, but skips LVGL's
  input device: hit-testing, pressed-state visuals, gestures and scrolling go
  untested.
- Commands land between frames plus a serial or Wi-Fi round trip, so a fast game
  becomes turn-based: fine for testing, not for play at speed.
- Over WebREPL, login reads the banner, so the stream starts after it, and a
  loop that never yields can't be interrupted. Passwords come from
  `~/.mpftp/webrepl-passwords.json`, as for every `ws://` command.
- `exec` refuses on `micropython.exe`: the pseudo console drops the Ctrl-D
  bytes that frame a raw-REPL result. `ask` (paste mode for several lines)
  works there.
- The app's timers keep firing while the interpreter waits in the raw REPL, as
  they do at the friendly prompt.
- `ble://` boards can't be held.
