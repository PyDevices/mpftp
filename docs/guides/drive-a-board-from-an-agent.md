# Drive a board from an AI coding agent

You can let an AI coding agent (Claude Code, Codex, Cursor and others) push
Python to a board, read its output, build firmware and flash it, using the
`mpftp` command. This page is for you, and for the agent: point it here, or
install the plugin below. The commands are the ones in the rest of this guide;
what is specific to an agent is how they share one session, and which commands
interrupt the program you are testing.

**Serial** works for both MicroPython and CircuitPython. **Official firmware
downloads** are MicroPython's; builds can be either.

## Give the agent the commands

For Claude Code, the [plugin](https://github.com/PyDevices/mpftp/tree/main/integrations)
installs a short skill that teaches the CLI and points back here:

```text
/plugin marketplace add PyDevices/mpftp
/plugin install mpftp@mpftp
```

Codex's Plugins panel reads the same plugin (**Plugins → Add → Add plugin
marketplace**, source `PyDevices/mpftp`). Other agents can read this page
directly, for example from your project's `AGENTS.md`. Install the CLI first
([Install](../get-started/install.md)); with a clone of the repository,
[`./scripts/mpftp`](../../scripts/mpftp) stands in for `mpftp`.

## How commands find a session

A serial port has one owner. When the editor is connected, the terminal
commands should share its session rather than open a second connection on the
same port. They look, in order, at:

| Place | What it is |
|---|---|
| `MPFTP_RPC` | An address such as `127.0.0.1:PORT`, if you need to pick one when several windows compete |
| `~/.mpftp/workspace-rpc.json` | A map from workspace folder to session; the folder you run in, or its parents, is matched. mpftp creates nothing inside your folders |
| (neither) | The command starts a private session of its own, and needs `-d DEVICE` |

Other files: `~/.mpftp/sessions/<id>.pid` is each window's claim
(`MPFTP_SESSION_ID`), so windows don't kill each other's session;
`~/.mpftp/activity.log` is NDJSON of connects, transfers, RPC calls and errors
(`mpftp watch` follows it); `~/.mpftp/repl.log` holds REPL I/O when a REPL is
open. The editor window must have mpftp loaded for the session to exist.

```bash
mpftp status          # is a session up? which device? which interpreter?
mpftp watch           # follow the activity log
```

**Two boards, two windows.** Each window runs its own session. Run the agent
from a folder inside the matching workspace so its commands reach that window,
and connect each window to a different port. A port stays exclusive: a second
connect gets "port busy". Reloading one window does not kill the other's
session.

**Two windows on the same folder** are a different case. The registry has one
entry per path, so the window that connected last wins and the other becomes
unreachable by folder, with no error. `mpftp status` shows `editor`, `pid` and
`updatedAt` on the entry it resolved, which say whose session it is
(`extension_running: true` alone does not). A logged `rpc_registry_collision`
in `activity.log` marks the moment one window's registration overwrote a
different, recent one.

On WSL, serial and ESP32 flashing use the Windows Python so `COM` ports work.
The editor's session uses the mpremote it bundles; the terminal CLI with no
editor running needs `mpremote` on that interpreter, and `circup` for
CircuitPython packages either way (`python.exe -m pip install mpremote circup`).

## Which commands interrupt

`exec`, `eval`, `run`, `put`, `get` and the other file commands enter the raw
REPL, which sends Ctrl-C first and stops whatever is running, including a
program you just started. When you are testing a running program, use these:

- [`mpftp hold`](drive-a-running-app.md) keeps one friendly REPL open across
  calls so you can talk to the app while it runs.
- `mpftp watch-repl` tails the board's own output and never sends Ctrl-C. Have
  the script `print()` its progress.
- `mpftp probe` runs a script, waits and reads a result file back in one
  command ([Run code and use the REPL](run-code-and-use-the-repl.md#watch-a-program-without-stopping-it)).

[Capture the console](capture-the-console.md) is for native crashes.

## Rules of thumb

- Debug with `exec`, `eval` or `run` before rewriting `main.py` or `code.py`.
- Soft-reset after bad imports; hard-reset if the port is wedged.
- Don't put secrets in scripts, since scripts show up in `activity.log`. Never
  print a WebREPL or Bluetooth password.
- Use [Wi-Fi](connect-over-wifi.md) or [Bluetooth](connect-over-bluetooth.md) to
  drive a board while serial is busy with `monitor`.
- File Transfer skips dotfiles and `__pycache__`; a CLI `put` of a single path
  does what you ask.
- Before a flash, run `firmware detect`. If secure boot or flash encryption is
  on, stop and confirm.

## Errors

A command that fails prints `{"ok": false, "error": "...", "hint": "..."}` to
stdout and exits non-zero. `hint` is present only when there is an actionable
next step that isn't already in `error`. A successful command's JSON has the
same shape, so parse stdout as JSON either way instead of branching on the exit
code first.

## Raw RPC

The editor's session speaks the same JSON-lines protocol as the private one,
plus the firmware methods:

```bash
mpftp rpc ping
mpftp rpc fs_listdir '{"path":"/"}'
```

[Troubleshooting](troubleshooting.md) has the symptom table.
