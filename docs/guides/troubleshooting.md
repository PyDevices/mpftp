# Troubleshooting

| Symptom | What to do |
|---|---|
| Listing ports shows nothing, or connect fails | Another tool may hold the port: close Thonny and serial monitors. After a bad flash the filesystem may be corrupt: erase and reflash |
| Port busy or exclusive lock | Another mpftp window owns that port. Disconnect there, or pick the other board. Two windows can't share one port |
| WSL can't see `COM` ports | Install a Windows Python with `mpremote`. mpftp needs no `usbipd` |
| Two windows, two boards | Use distinct ports and run the command from each workspace folder. `mpftp status` should show distinct `rpc` and `session_id` |
| The terminal reaches an unexpected session | Two windows on one folder: the last connect wins. `mpftp status` shows `editor`, `pid` and `updatedAt` on the resolved entry, and `activity.log` has an `rpc_registry_collision` line |
| `Access is denied` or `transport_dead` after a hung `exec` or `run` | The session releases the port handle on its own. `disconnect`, then `resume` or `connect`. If it is still busy, reload the editor window, and replug USB last |
| `busy or locked` on a board's native USB port with no tool holding it | The board's program is stuck in a callback that never returns, so its USB stops answering. A killed mpftp command lets go of the port after a few seconds. If it stays busy, reset the board through its UART port (`hard-reset -d <UART>`) or its RESET button |
| `get` failed with "source code string cannot contain null bytes" | An older mpftp. The board's native USB sent extra bytes after a chunk, sometimes holding a `\x04` that ends the reply early. A current `get` and `cp` read each chunk with an explicit `seek`, check its length and ask again; a chunk that never comes right fails with its byte offset |
| `timeout waiting for first EOF` | The board is still running your program. Use `run` without `--follow` or `exec --no-follow`, then `interrupt` or `soft-reset` |
| Board is dead after deploy and `soft-reset` | Expected: soft-reset skips `main.py`. Use `soft-reboot` or `hard-reset` |
| Two USB ports (UART and native CDC) | `mpftp ports` shows `role` (`repl` or `cdc_debug`). Control on the UART and `monitor` on the CDC |
| A crash or reboot with no Python traceback | The panic backtrace goes to the console UART only: [Capture the console](capture-the-console.md) |
| `monitor` log is empty | `debug-tee` from the terminal dies with the command; use `monitor`. If `monitor` is still silent, the console is on the other port, or nothing is printed |
| `could not enter raw repl` after flash | Run `firmware detect`, then erase and reflash. A corrupt filesystem boot-loops and blocks soft-reset |
| Wrong board, or no Wi-Fi on a P4 | `firmware detect`, and pick the `C5_WIFI` or `C6_WIFI` variant explicitly |
| Build can't find micropython-pydevices | Run from the folder holding it, or pass `--build-system PATH`; see [Build firmware](build-firmware.md#get-the-build-system) |
| Build can't find a cross-compiler | Install the one the error names, or use **Locate…** for its `bin/` folder in the Firmware panel |
| App partition too small | `build_mp.py` grows it and rebuilds once; `--no-autosize` prints the layout that would fit |
| A module is missing from the firmware | `firmware modules` should list it, and `--modules` must name it (or `all`, unless it is opt-in) |
| CircuitPython: every command hangs and serial writes time out | The board is wedged with the receive ring full and Ctrl-C can't reach it. [1200-baud touch](recover-a-board.md#when-ctrl-c-cant-reach-the-board), then copy firmware |
| CircuitPython: wrote to the drive and the board didn't restart | `supervisor.runtime.autoreload` is probably `False`; reset explicitly |
| CircuitPython: the display blanks when the script ends | Expected. The app has to hold the VM |

More on a specific area: [Recover a board](recover-a-board.md),
[Work with CircuitPython](work-with-circuitpython.md),
[Connect over Wi-Fi](connect-over-wifi.md).
