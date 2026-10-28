# Command line

Every command is `mpftp COMMAND [options]`; `python -m mpftp COMMAND` is the
same. `mpftp COMMAND --help` lists a command's options. Board commands take
`-d DEVICE` (also `--device`) after the command name: a serial port
(`COM4`, `/dev/ttyACM0`), `ws://HOST[:8266]` for WebREPL, or `ble://NAME` for a
`bledev` board. With an editor session connected and no `-d`, a command uses
that session. A failure prints `{"ok": false, "error": "...", "hint": "..."}`
and exits non-zero.

## Session and connection

| Command | What it does |
|---|---|
| `status` | Whether a session is up, which device, which interpreter |
| `ports` | List serial ports; `role` says `repl` or `cdc_debug` |
| `connect`, `disconnect`, `resume` | Connect, let go, reconnect to the last device |
| `watch` | Tail the activity log (`--repl` for the REPL log) |
| `rpc METHOD [JSON]` | Call a session method directly (`rpc ping`) |

## Files

| Command | What it does |
|---|---|
| `ls`, `tree`, `df` | List, tree and free space |
| `put LOCAL REMOTE` | Upload. `-r` for a folder, `--no-verify` to skip SHA-256, `--mpy` to compile |
| `get REMOTE LOCAL` | Download, same options |
| `cp SRC DST` | Copy; `:` before a path marks the board side |
| `hash`, `mkdir`, `rm`, `touch`, `rename` | As named. `rm -r` removes a tree |
| `edit` | Edit a board file with `$EDITOR` |
| `clean` | Remove debris a session leaves (probe scratch files, `.bak`, `__pycache__`) |
| `mount`, `umount`, `romfs` | MicroPython over serial only |

## Run and reset

| Command | What it does |
|---|---|
| `eval`, `exec` | Evaluate an expression, or run code; `exec --no-follow` for loops |
| `run FILE` | Run a local script; `--follow` waits for it to end, giving up after 10 s with no output. `--timeout SECONDS` changes that, and `--timeout 0` waits until the script ends |
| `probe FILE` | Run, wait, and read a result file (`--reboot-first`, `--capture`, `--wait`) |
| `watch-repl` | Tail the board's output without interrupting it |
| `interrupt`, `soft-reset`, `soft-reboot`, `hard-reset` | See [Run code and use the REPL](../guides/run-code-and-use-the-repl.md#interrupt-and-reset). `hard-reset --monitor SECONDS [--log-path FILE]` captures the boot |
| `bootloader` | Enter the bootloader |
| `rtc [--set]` | Read the board clock, or set it to the host's UTC |
| `monitor PORT` | Read-only console capture (`--seconds`, `--log-path`) |
| `debug-tee` | Read-only tee of a second port that dies with the command; prefer `monitor` |
| `usb-restart` | Windows: restart a wedged ESP32 or CH343 USB node (`--status`, `--list`, `--instance`) |

## Packages

| Command | What it does |
|---|---|
| `mip PKG` | Install with mip (MicroPython); `--target` defaults to `/lib`, `--index` names an index |
| `circup PKG` | Install with circup (CircuitPython); `--target` defaults to `/lib` |
| `search WORDS` | Search the package indexes. `--source`, `--interpreter`, `--limit` (25), `--refresh`, `--text` |

## Wi-Fi

`wifi boards`, `find NAME`, `password BOARD`, `status`, `enable`, `disable`: see
[Connect over Wi-Fi](../guides/connect-over-wifi.md).

## `mpftp hold`

Holds one device's friendly REPL open across commands. See
[Drive a running app](../guides/drive-a-running-app.md).

| Command | What it does |
|---|---|
| `start` | Opens the connection and stays (`-d DEVICE`, or `--spawn CMD` with `--cwd`, `--env NAME=VALUE`, `--name`). Sends nothing: no Ctrl-C, no reset, no raw REPL. Refuses a board that is already held |
| `ask CODE` | Types the code, waits for the prompt after it, and returns what came back. `--marker @@` also returns the text after `@@` on each line, `--file` reads the code from a file, `--timeout` defaults to 10 s. Exit code 2 if no prompt came |
| `send TEXT` | Types it (Enter is added unless `--no-enter`; `--ctrl C` sends one control key) and returns at once, with the stream offset it went at |
| `read` | What the device printed since the last `read` or `ask`, or since `--since N`. `--wait` and `--until REGEX` wait for it |
| `interrupt` | Ctrl-C, then waits for the prompt |
| `exec CODE` | Runs the code through the raw REPL and returns to the friendly REPL. The one command that leaves it |
| `stop`, `status` | Lets go (asks the holder, then the PID from its PID file), or shows holders and offsets |

Every answer is JSON with a `next` offset; `--text` prints only the answer.

## `mpftp firmware`

See [Firmware build options](firmware-build-options.md).
