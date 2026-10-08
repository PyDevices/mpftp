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

Firmware download/build/flash is **MicroPython-only**. Official **Download** mode needs no local checkout.

**Build** mode needs a **firmware workspace**: a folder that contains `micropython/` (directory or symlink) or that *is* the MicroPython tree (`ports/` and `py/`).
Optional in that workspace:

- Module repositories beside `micropython/`, which the Modules card lists for you to tick; see [firmware-modules.md](firmware-modules.md)
- Any **port dependency** trees you need (for example `esp-idf`, `emsdk`) as directories or symlinks

Dependencies that are not in the workspace must be provided via their environment variables (for example `IDF_PATH`, `EMSDK`) or the Locate… prompt when you build.

Discovery order for MicroPython: settings → `MP_DIR` → `~/micropython` → editor open folders → Choose workspace….

### Download vs Build

| Mode | Use when |
|------|----------|
| **Download** | You want an official micropython.org binary (Thonny catalog) |
| **Build** | You have a MicroPython tree and want a custom firmware (user modules, partitions, …) |

**Detect** uses esptool first (works on a bare board), then optionally enriches from a live MicroPython session.

Choosing modules and presets: **[firmware-modules.md](firmware-modules.md)**.

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
  board or your disk. Double-clicking a local file uploads it, as in VS Code.
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
- **Build missing a tree:** set the env var, symlink the repo under the firmware workspace, or use Locate….
- **ESP-IDF version mismatch:** Install instructions follow the version recommended in `ports/esp32/README.md` for your checkout.
- **App partition too small:** autosize grows `esp32_partitions/<board>.csv` and rebuilds once (see [Autosize](#esp32-partition-autosize)); use `--no-autosize` only if you are managing the table yourself.

## More

- [Developers guide](developers-guide.md) — architecture, discovery contract, packaging
