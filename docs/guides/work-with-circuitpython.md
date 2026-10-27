# Work with CircuitPython

mpftp drives both interpreters, but they differ enough that assuming parity
will cost you time. The sections below are the differences that matter.

## Resets

| | MicroPython | CircuitPython |
|---|---|---|
| `soft-reset` | Raw soft reset, skips `main.py` | Toggles friendly and raw REPL, does **not** run `code.py` |
| `soft-reboot` | Ctrl-D, runs `main.py` | Ctrl-D, runs `code.py` |
| Packages | `mip` | `circup` |

CircuitPython may show "Press any key to enter the REPL…"; mpftp sends a key
before raw. Prefer the CDC REPL port; CDC2 data interfaces are filtered.
`mount`, `umount` and `romfs` are MicroPython-only.

## File operations don't interrupt the running program

With the **CIRCUITPY** drive mounted, `put`, `get`, `ls` and `hash` go through
the drive, not the serial port, and say so:

```bash
mpftp put -d PORT ./probe.py /probe.py
# {"path": "/probe.py", "size": 18, "via": "circuitpy_msc"}
```

On MicroPython every `exec`, `get` and `put` enters the raw REPL and Ctrl-Cs
whatever is running, so reading a file to see how a long script is doing kills
it. On CircuitPython you can read the file. The first `connect` still
interrupts once, because the interpreter has to be detected before mpftp can
route. Under WSL, `/mnt/d` does not exist (Windows doesn't auto-mount removable
drives into WSL), so the sidecar uses the Windows path with Windows Python.

`boot_out.txt` on the drive identifies the board without any serial contact, and
its `UID` equals the port's `serial_number`, so you can match a drive to a port
offline.

## The board can't write its own filesystem

While USB storage is exposed, `CIRCUITPY` is writable by the host and read-only
to the board, so a script that appends progress to `/result.txt` fails. Print
instead, or read the traceback afterwards:

```python
import supervisor
supervisor.get_previous_traceback()   # from the previous code.py run
```

`supervisor.set_next_code_file()` chooses what runs next.

## Don't assume auto-reload

`supervisor.runtime.autoreload` is often `False` (CircuitPython prints
`Auto-reload is off.` at the top of each run), so writing to the drive does not
restart the board. Check it rather than relying on a write to trigger a run.

## The display blanks when the script ends

When the VM finishes, the supervisor runs `reset_port()`, releasing pins and
resetting the panel. An app that draws has to hold the VM.

## Ctrl-C is not an interrupt inside `atexit`

CircuitPython doesn't arm Ctrl-C as an interrupt character while an `atexit`
handler runs; it arrives as ordinary input. A handler that loops without
reading stdin fills the USB CDC receive ring, and then host writes time out and
no tool can reach the board. If you write board code that holds the VM this way,
drain the ring each pass:

```python
import sys, supervisor
rt = supervisor.runtime
waiting = rt.serial_bytes_available
if waiting and "\x03" in sys.stdin.read(waiting):
    ...   # treat as quit
```

A board already wedged this way needs the [1200-baud touch](recover-a-board.md#when-ctrl-c-cant-reach-the-board).

## Build CircuitPython-compatible firmware

`mpftp firmware build --interpreter circuitpython`; see
[Build firmware](build-firmware.md#build-circuitpython-compatible-firmware).
