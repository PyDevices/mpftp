# mpftp

**MicroPython and CircuitPython board tools for VS Code and compatible derivatives**

`mpftp` brings microcontroller board management directly into your modern code editor (VS Code, Google Antigravity IDE, Cursor, VSCodium):
- **An In-Editor Alternative to Thonny**: Edit board files, transfer files, install packages (`mip` / `circup`), and flash firmware without switching back and forth to external IDEs or standalone tools.
- **Embedded Serial REPL**: The interactive REPL opens directly inside your editor's **Terminal panel**, allowing you to test and inspect code live alongside your editor tabs and AI coding agents.
- **Wi-Fi Fast MIP Transfers**: Transferring `wifi.py` and creating `secrets.py` on Wi-Fi enabled boards enables on-board `mip` package installation directly over the network, which is significantly faster than serial file transfers.
- **Visual Firmware Builder**: A GUI companion for firmware workspaces (such as [cmods](https://github.com/PyDevices/cmods)), allowing you to download official releases or build and flash custom firmware from the UI.
- **Completely Optional**: If you are already comfortable with command-line tools (`mpremote`, `esptool`, `circup`) or standalone IDEs, you can continue using them. `mpftp` is provided as an all-in-one in-editor workbench.

Published as **`pydevices.mpftp`** under [PyDevices](https://github.com/PyDevices).

## Documentation

- **[User guide](docs/user-guide.md)** — getting started, File Transfer, REPL, Firmware workspace, autosize, troubleshooting
- **[Aggregator & user modules](docs/aggregator.md)** — workspace `micropython.cmake` / `manifest-micropython.py` contract
- **[Developers guide](docs/developers-guide.md)** — architecture, discovery contract, packaging, contribution
- **[AGENTS.md](AGENTS.md)** — agent/CLI workflows: board ops, flash recovery, pointers to aggregator docs

## Features

- **Dual-Pane File Transfer**: Local ↔ board file management (upload, download, mkdir, new file, delete, rename, drag-and-drop)
- **Live In-Editor Editing**: Open and edit files directly on the board with automatic save-back and optional SHA-256 verification
- **Integrated Terminal REPL**: Hardware serial REPL embedded right in the editor Terminal workspace
- **Automatic Runtime Detection**: Connects over serial, automatically identifies MicroPython or CircuitPython, and synchronizes the RTC
- **Package Installation**: Install packages with `mip` (MicroPython) or `circup` (CircuitPython)
- **Visual Firmware Workbench**: Detect board hardware, download official MicroPython builds, or build and flash custom firmware workspaces (ESP32, RP2040, SAMD)
- **AI Agent-Friendly**: Local TCP RPC and agent CLI interface sharing the active editor session


## Requirements

- VS Code or compatible derivative (such as Google Antigravity IDE, Cursor, VSCodium) (engine `^1.85.0`)
- Python 3 with [`mpremote`](https://pypi.org/project/mpremote/)
  - WSL / Windows serial: Windows Python + `pip install mpremote`
  - Native Linux: venv or `mpftp.pythonPath`
- For CircuitPython packages: [`circup`](https://pypi.org/project/circup/) on the **same** Python (`pip install circup`)

## Install

Marketplace: search for **mpftp** by **pydevices**, or install a `.vsix`:

```bash
npm install
npm run package
# Extensions: Install from VSIX… → mpftp-*.vsix
```

Development (Cursor Remote-WSL):

```bash
npm install && npm run compile
./scripts/install-cursor-wsl.sh
# Developer: Reload Window
```

## Quick start

1. **mpftp: Connect to Board** — pick a port
2. Open **File Transfer** — move files; open **REPL** for the shell
3. **Install Package** — mip (MicroPython) or circup (CircuitPython) by detected runtime
4. **Firmware** — Detect a board, then Download an official MicroPython image or Build from a firmware workspace

A **firmware workspace** is a folder that contains `micropython/` (or *is* the MicroPython tree). Port SDKs go in that workspace as directories/symlinks, or via environment variables — see the [user guide](docs/user-guide.md).

## Commands (selection)

| Command | Action |
|---------|--------|
| `mpftp: Connect to Board` | Port picker; interrupt + runtime-aware clean |
| `mpftp: Resume Last Device` | Reconnect previous port |
| `mpftp: Open File Transfer in Panel / Editor` | Dual-pane UI |
| `mpftp: Open REPL` | ANSI terminal |
| `mpftp: Build & Flash Firmware…` | MicroPython firmware panel |
| `mpftp: Interrupt` / Soft Reset / Hard Reset | Board control |
| `mpftp: Install Package` | mip or circup by runtime |

## Settings (selection)

| Setting | Purpose |
|---------|---------|
| `mpftp.workspacePath` | Firmware workspace (MicroPython + optional SDK trees) |
| `mpftp.pythonPath` | Sidecar Python (empty on WSL → Windows `python.exe`) |
| `mpftp.buildPythonPath` | Native Python for builds |
| `mpftp.verifyTransfers` | SHA-256 after file transfer |
| `mpftp.autoReconnectAfterReset` | Reconnect after hard reset |

Full list: VS Code Settings → search `mpftp`, or [user guide](docs/user-guide.md).

## License

MIT — see [LICENSE](LICENSE) if present in the package, otherwise the repository license file.
