# mpftp User Guide

## What is mpftp?

**mpftp** is a VS Code / Cursor extension for working with
[MicroPython](https://micropython.org/) and [CircuitPython](https://circuitpython.org/)
boards over USB serial. It gives you a dual-pane file transfer UI, an ANSI REPL,
interpreter-aware package install (`mip` / `circup`), and a guided Firmware panel for
downloading or building and flashing board images: MicroPython, or
CircuitPython-compatible ones built with micropython-pydevices' `build_mp.py`.

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
- Python 3.9 or newer. [`mpremote`](https://pypi.org/project/mpremote/) and
  pyserial come bundled in the extension, so you don't install them.
  - **Windows / WSL:** a Windows Python, which can open `COM` ports
  - **Native Linux:** the system `python3`, or set `mpftp.pythonPath`
- For CircuitPython libraries: [`circup`](https://pypi.org/project/circup/) on the **same** interpreter
  (`python.exe -m pip install circup` on WSL/Windows)

### Install

1. Not yet on the VS Code Marketplace or Open VSX. Download the `.vsix` from
   the [latest GitHub release](https://github.com/PyDevices/mpftp/releases/latest)
   and install it (**Extensions: Install from VSIX…**), or build it from
   source (`cd extension && npm install && npm run package`).
2. Reload the window if prompted.
3. Click the **mpftp** status bar item or run **mpftp: Connect to Board**.

### Connect and transfer files

1. Connect and pick a serial port (`COM4`, `/dev/ttyACM0`, …). Prefer the CircuitPython **REPL** CDC interface (CDC2 data is filtered).
2. Open **File Transfer** (editor tab or panel).
3. Drag files between local and board panes, or use the header actions.
4. Double-click a board file to edit it; save writes it back (optional SHA-256 verify).
5. To save a copy somewhere else, run **mpftp: Save As…** (the title bar's
   save-as button, or right-click in the editor). Pick **This computer** or
   **Board**, then edit the path, which starts in that list's current
   folder. The editor then shows the copy, so the next save goes there and
   the original stays as it was. It asks before replacing a file, and the
   folder has to exist. VS Code's own Save As still saves only to this
   computer.

### Over Wi-Fi

Connect over USB once and run **mpftp: Enable Wi-Fi Access** (the PWA's
**Wi-Fi access…** button, or `mpftp wifi enable -d COM4`). It shows you the
change to `boot.py` and writes nothing until you say yes. After that the board
joins your network at every reset, and the Connect list offers it under
**Wi-Fi** by name. The first connect asks for its WebREPL password (at most 9
characters) and remembers it for that board.

The command line, the PWA and agents keep passwords in
`~/.mpftp/webrepl-passwords.json` (plaintext, readable only by you). VS Code
keeps its own in its secret storage and reads that file too. When you type a
password in VS Code, it asks once whether the others may have it; say yes and
an agent can reach the board you just set up. The setting
`mpftp.sharePasswords` (`ask`, `always`, `never`) answers for every board.

From the CLI, any board command takes `-d ws://BOARD-IP`. `mpftp wifi boards`
lists the boards mpftp remembers, and `mpftp wifi find NAME` looks one up by
its `.local` name. What each piece does, where passwords live, and what's
serial-only: [mpftp over Wi-Fi](plans/wifi-webrepl.md).

### Over Bluetooth

A board running pydevices' `bledev` can be reached over BLE, with no cable to
this machine: any CLI board command takes `-d ble://NAME`, where NAME is the
name the board advertises. The board serves it from `main.py`, e.g.
`bledev.filetransfer.start(password="...", name="rack")`, and the password
comes from `MPFTP_BLE_PASSWORD` (or `mpftp wifi password ble://NAME`). A
board started with `pairing="passkey"` needs this computer paired once: run
`python -m bledev.bleak pair NAME` and type in the passkey the board shows
(or use Windows Settings, Add device); after that mpftp uses the bond, with no
password if the board has none. A board with `pairing="justworks"` is paired
by mpftp itself.

In VS Code and the PWA, pick **Look for Bluetooth boards…** in the Connect
list. It lists what's advertising bledev's REPL nearby, strongest signal
first; pick one, or type the name. The first connect asks for the password if
the board wants one, and mpftp keeps it for that board. Details and speeds:
[mpftp over Bluetooth](plans/ble.md).

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

Official **Download** mode needs no local checkout.

**Build** mode builds with [micropython-pydevices](https://github.com/PyDevices/micropython-pydevices)' `build_mp.py`, so it needs a micropython-pydevices checkout. `curl -fsSL https://pydevices.github.io/install.sh | sh` clones one. mpftp finds it in your workspace folder, beside your MicroPython checkout, in `~/micropython-pydevices`, or wherever the `buildSystemPath` setting says. `build_mp.py` fetches MicroPython, the modules and the port's toolchain itself on its first build.

The Target card lists `build_mp.py`'s ports, boards and variants, and the Modules card its modules, one checkbox each. Targets, modules, CircuitPython-compatible builds and every option are in **[Building firmware](firmware-modules.md)**.

### Download vs Build

| Mode | Use when |
|------|----------|
| **Download** | You want an official micropython.org binary (Thonny catalog) |
| **Build** | You want custom firmware: our modules compiled in, a board variant of ours, CircuitPython-compatible |

**Detect** uses esptool first (works on a bare board), then optionally enriches from a live MicroPython session.

### ESP32 partition autosize

When an esp32 image is larger than its app partition, `build_mp.py` grows the
partition, moves the ones after it, and builds once more, then prints the new
layout. rp2 gets the same treatment by shrinking the filesystem. Pass
`--no-autosize` to `mpftp firmware build` to refuse instead and print the
layout that would fit. Either way the filesystem moves, so a board flashed with
the new layout comes up with an empty one.

If the on-device partition layout differs from the artifact at flash time, mpftp
**stops and warns** instead of erasing automatically. Enable **Erase flash before
writing** and click **Flash** again. A full erase wipes the filesystem
(**vfs** / storage) partition — all files on the board will be lost.

## Settings (high level)

| Setting | Purpose |
|---------|---------|
| `mpftp.workspacePath` | Firmware workspace (where mpftp looks for micropython-pydevices and MicroPython) |
| `mpftp.micropythonPath` | Optional override of the MicroPython tree |
| `mpftp.idfPath` / `mpftp.emsdkPath` | Optional SDK overrides |
| `mpftp.pythonPath` | Serial/sidecar Python (on WSL, leave empty for Windows `python.exe`) |
| `mpftp.buildPythonPath` | Native Python for the build engine |
| `mpftp.verifyTransfers` | SHA-256 after transfers |
| `mpftp.compileOnUpload` | Compile `.py` to `.mpy` via `mpy-cross` on upload (MicroPython only) |
| `mpftp.autoReconnectAfterReset` | Reconnect after hard reset |

## The browser interface (no editor required)

`python -m mpftp` opens mpftp in your browser: the same File Transfer panel
as the VS Code extension, the REPL under it, and an editor beside them. You
don't need VS Code, and you can install it as an app with its own window and
icon.

Start it in the folder you want on the Local side:

```bash
pip install -i https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ pydevices-mpftp
cd ~/projects/blink
python -m mpftp
```

It serves itself at `http://127.0.0.1:8317/` and opens a browser tab. Stop it
with Ctrl+C. To pick another port or skip the tab, run
`python -m mpftp.pwa --port 9000 --no-open` (`MPFTP_PWA_PORT` sets the port
too). `python -m mpftp <subcommand> ...` with any arguments is still the
regular CLI.

What you can do there:

- **Connect** with the plug button. Pick a USB serial port, a Wi-Fi board
  mpftp remembers, an address you type, or a Bluetooth board. The status
  line reads `Connected · COM4` and the badge shows MicroPython or
  CircuitPython.
- **Move files** between the two lists: select on either side (Ctrl+click
  and Shift+click pick several, folders included) and press an arrow, or
  drag from one list to the other. Folders go with everything inside them,
  except `.git`, `__pycache__`, `*.pyc` and other dot-entries. Each file is
  checked with SHA-256 afterwards (`verifyTransfers` in
  `~/.mpftp/config.json` turns that off).
- **Manage files** on either side with the buttons over each list or a
  right-click: new folder, new file, rename, delete. Delete asks first.
- **Edit** a file: double-click a board file, or select a file on either
  side and press Open in Editor. Each file gets a tab (board files have a
  blue dot), and Ctrl+S or Save writes it back to where it came from, the
  board or your disk. Save As (Ctrl+Shift+S) saves a copy on this computer
  or the board, starting in the folder that list is showing, and the tab
  follows the copy. Double-clicking a local file uploads it, as in VS Code.
- **Run** code: the play button runs a board `.py`, or uploads and runs a
  local one, and its output appears in the REPL. The `⋯` menu has the rest:
  interrupt, soft and hard reset, eval and exec, `mip`/`circup` installs,
  the RTC, `df`, Wi-Fi access and running the editor's file.
- **Install it as an app**: Chrome and Edge offer Install in the address bar.
  The app still needs `python -m mpftp` running to reach your boards.

The Local list is the computer running `python -m mpftp`, which is also the
only place the page can be opened from: it listens on loopback. It is a
board session of its own, separate from the VS Code extension's, so connect
a board in one or the other, not both.

**Flash firmware** onto an ESP board with the Firmware button. Choose the
firmware `.bin` on your computer (the combined image, such as MicroPython's
`ESP32_GENERIC_S3-...bin`), check the serial port, which starts as the
connected board's, and press Flash. Tick "Erase all of flash first" for a
clean board; it also deletes the files on it. mpftp lets go of the board,
writes the image with esptool at the offset its chip needs (0x1000 on the
ESP32 and S2, 0x2000 on the P4 and C5, 0x0 on the rest), shows the progress,
and reconnects once the board restarts. A board whose REPL is its own USB
(an S2 or S3 on native USB) is put into its bootloader first; if no download
port appears, hold BOOT, tap RESET, choose the new port and Flash again.

The Firmware button only flashes ESP boards. For UF2 boards, drag the `.uf2`
onto the board's drive. Downloading and building firmware are in the
extension's Firmware panel and `mpftp firmware ...`.

## Troubleshooting

- **Listing ports then nothing / connect fails:** another tool may hold the port; close it. After a bad flash, the filesystem may be corrupt — erase and reflash.
- **WSL cannot see COM ports:** ensure Windows Python + `mpremote` are installed; mpftp should not need `usbipd`.
- **Build can't find micropython-pydevices:** run mpftp from the folder that holds it, set `buildSystemPath`, or pass `--build-system`; see [Building firmware](firmware-modules.md#getting-the-build-system).
- **Build missing a cross-compiler:** install the one the error names, or use Locate… for its `bin/` folder.
- **App partition too small:** `build_mp.py` grows it and rebuilds once (see [Autosize](#esp32-partition-autosize)); `--no-autosize` refuses instead.

## More

- [Developers guide](developers-guide.md) — architecture, discovery contract, packaging
