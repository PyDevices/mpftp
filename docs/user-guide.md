# mpftp User Guide

## What is mpftp?

**mpftp** is a VS Code / Cursor extension for working with
[MicroPython](https://micropython.org/) and [CircuitPython](https://circuitpython.org/)
boards over USB serial. It gives you a dual-pane file transfer UI, an ANSI REPL,
interpreter-aware package install (`mip` / `circup`), and a guided Firmware panel for
downloading or building and flashing **MicroPython** board images (firmware is
MicroPython-only).

It is maintained under the [PyDevices](https://github.com/PyDevices) organization.

## What problem it solves

MicroPython and CircuitPython boards are usually driven with several separate tools:
a serial terminal, `mpremote` / MSC / `circup`, and a makefile/IDF/emsdk toolchain for
firmware. Switching between them is slow, and on WSL the host often cannot see Windows
`COM` ports without extra USB bridging.

mpftp puts connect, files, REPL, and MicroPython firmware in one extension host session,
with a single serial ownership model so the UI and agents do not fight over the port.

## How it works

- A long-lived **Python sidecar** (`mpremote`-backed) owns the serial link and speaks JSON-lines to the extension.
- Connect detects **`micropython`** vs **`circuitpython`** and adjusts soft-reset / package install.
- **File Transfer** and **REPL** talk to that session.
- **Firmware** build/flash runs as a separate host-side Python engine so compiles never hold the serial port longer than needed (MicroPython only).
- On **WSL**, serial and esp32 flash use **Windows Python** so `COM` ports work without `usbipd`.

## Getting started

### Requirements

- VS Code or Cursor
- Python 3 with [`mpremote`](https://pypi.org/project/mpremote/)
  - **Windows / WSL:** install for Windows Python (`python.exe -m pip install mpremote`)
  - **Native Linux:** `python3 -m venv .venv && .venv/bin/pip install mpremote`, or set `mpftp.pythonPath`
- For CircuitPython libraries: [`circup`](https://pypi.org/project/circup/) on the **same** interpreter
  (`python.exe -m pip install circup` on WSL/Windows)

### Install

1. Install the extension from the Marketplace (`pydevices.mpftp`) or from a `.vsix` (**Extensions: Install from VSIX…**).
2. Reload the window if prompted.
3. Click the **mpftp** status bar item or run **mpftp: Connect to Board**.

### Connect and transfer files

1. Connect and pick a serial port (`COM4`, `/dev/ttyACM0`, …). Prefer the CircuitPython **REPL** CDC interface (CDC2 data is filtered).
2. Open **File Transfer** (editor tab or panel).
3. Drag files between local and board panes, or use the header actions.
4. Double-click a board file to edit it; save writes it back (optional SHA-256 verify).

### Soft reset and packages

| Interpreter | Soft Reset | Soft Reboot | Install Package |
|---------|------------|-------------|-----------------|
| MicroPython | Raw soft-reset (skips `main.py`) | Ctrl-D (runs `main.py`) | **mip** → `/lib` by default |
| CircuitPython | Friendly↔raw toggle (does **not** run `code.py`) | Ctrl-D (runs `code.py`) | **circup** — CIRCUITPY `--path` when mounted, else Web Workflow if writable, else USB staging |

File `put` / `cp` on CircuitPython also prefer the mounted **CIRCUITPY** drive
(USB MSC) when present; serial writes are used when the drive is not mounted.

Enable `mpftp.compileOnUpload` to compile `.py` uploads to `.mpy` via
`mpy-cross` (MicroPython only; `boot.py`/`main.py` stay source). Requires
`mpy-cross` in the firmware workspace build or on `PATH`.

Use **mpftp: Install Package** in the UI, or CLI `mpftp mip …` / `mpftp circup …`.

### REPL

**mpftp: Open REPL** opens a terminal attached to the same session (ANSI colors supported). Interrupt / soft reset / soft reboot / hard reset are available as commands. After a hung `exec`/`run`, mpftp releases a dead COM handle (`transport_dead` in the activity log) so Connect/Resume can reclaim the port.

### Firmware workspace (Build)

Firmware download/build/flash is **MicroPython-only**. Official **Download** mode needs no local checkout.

**Build** mode needs a **firmware workspace**: a folder that contains `micropython/` (directory or symlink) or that *is* the MicroPython tree (`ports/` and `py/`).
Optional in that workspace:

- `micropython.cmake` / `manifest-micropython.py` — aggregators for user modules and frozen Python (Create stubs… if missing); see [aggregator.md](aggregator.md)
- Any **port dependency** trees you need (for example `esp-idf`, `emsdk`) as directories or symlinks

Dependencies that are not in the workspace must be provided via their environment variables (for example `IDF_PATH`, `EMSDK`) or the Locate… prompt when you build.

Discovery order for MicroPython: settings → `MP_DIR` → `~/micropython` → editor open folders → Choose workspace….

### Download vs Build

| Mode | Use when |
|------|----------|
| **Download** | You want an official micropython.org binary (Thonny catalog) |
| **Build** | You have a MicroPython tree and want a custom firmware (user modules, partitions, …) |

**Detect** uses esptool first (works on a bare board), then optionally enriches from a live MicroPython session.

User modules / aggregators: **[aggregator.md](aggregator.md)**.

### ESP32 partition autosize

esp32 builds can fail when the firmware image is larger than the app (`factory`)
partition in the board’s partition table. mpftp handles that automatically:

1. Parse the ESP-IDF error (`app partition is too small … (overflow …)`).
2. Grow the app partition (aligned) and reflow following partitions.
3. Write the override to **`<firmware-workspace>/esp32_partitions/<board>.csv`**
   (or `<board>-<variant>.csv`). The MicroPython checkout is **not** modified.
4. Point the build-dir `sdkconfig` at that CSV (path relative to `ports/esp32`:
   `../../../esp32_partitions/…`) and rebuild **once**.

There is no manual partition slider in the Firmware UI. Scripted overrides remain
available via `./scripts/mpftp firmware partitions …`. Pass `--no-autosize` on
the build engine/CLI to disable the automatic grow-and-retry.

If the on-device partition layout differs from the artifact at flash time, mpftp
**stops and warns** instead of erasing automatically. Enable **Erase flash before
writing** and click **Flash** again. A full erase wipes the filesystem
(**vfs** / storage) partition — all files on the board will be lost.

## Settings (high level)

| Setting | Purpose |
|---------|---------|
| `mpftp.workspacePath` | Firmware workspace (MicroPython + optional SDK symlinks) |
| `mpftp.micropythonPath` | Optional override of the MicroPython tree |
| `mpftp.idfPath` / `mpftp.emsdkPath` | Optional SDK overrides |
| `mpftp.pythonPath` | Serial/sidecar Python (on WSL, leave empty for Windows `python.exe`) |
| `mpftp.buildPythonPath` | Native Python for the build engine |
| `mpftp.verifyTransfers` | SHA-256 after transfers |
| `mpftp.compileOnUpload` | Compile `.py` to `.mpy` via `mpy-cross` on upload (MicroPython only) |
| `mpftp.autoReconnectAfterReset` | Reconnect after hard reset |

## Local PWA (no editor required)

`pip install pydevices-mpftp` also gets you a small local web app — file
transfer and REPL, no VS Code/Cursor needed:

```bash
python -m mpftp
```

Serves itself at `http://127.0.0.1:8317/` (override with `--port`) and opens
a browser tab automatically (`--no-open` to skip that). It's installable as
a PWA from the browser's install prompt, and works offline for the app
shell itself — the board session obviously still needs the page open and
the server running. `python -m mpftp <subcommand> ...` (any arguments)
still runs the regular CLI, unchanged; only the bare no-argument form
launches the PWA.

This is a separate, explicitly-launched loopback service — it does not
share a session with the VS Code extension's agent RPC, and connecting a
board in one does not make it visible in the other. Firmware build/flash
isn't in the PWA yet; use the extension's Firmware panel or `mpftp firmware
...` for that.

## Troubleshooting

- **Listing ports then nothing / connect fails:** another tool may hold the port; close it. After a bad flash, the filesystem may be corrupt — erase and reflash.
- **WSL cannot see COM ports:** ensure Windows Python + `mpremote` are installed; mpftp should not need `usbipd`.
- **Build missing a tree:** set the env var, symlink the repo under the firmware workspace, or use Locate….
- **ESP-IDF version mismatch:** Install instructions follow the version recommended in `ports/esp32/README.md` for your checkout.
- **App partition too small:** autosize grows `esp32_partitions/<board>.csv` and rebuilds once (see [Autosize](#esp32-partition-autosize)); use `--no-autosize` only if you are managing the table yourself.

## More

- [Developers guide](developers-guide.md) — architecture, discovery contract, packaging
