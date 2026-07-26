# mpftp

**MicroPython and CircuitPython board tools for VS Code and Cursor**

Connect over USB serial, transfer files in a dual-pane UI, use a full ANSI REPL,
install packages (`mip` / `circup`), and download or build/flash **MicroPython**
firmware — on Linux, Windows, and WSL (COM ports via Windows Python).

Published as **`pydevices.mpftp`** under [PyDevices](https://github.com/PyDevices).

## Documentation

- **[User guide](docs/user-guide.md)** — getting started, File Transfer, REPL, Firmware workspace, autosize, troubleshooting
- **[Aggregator & user modules](docs/aggregator.md)** — workspace `micropython.cmake` / `manifest.py` contract
- **[Developers guide](docs/developers-guide.md)** — architecture, discovery contract, packaging, contribution
- **[Publishing](docs/PUBLISHING.md)** — tag-based releases (`vX.Y.Z`) and VSIX publish
- **[AGENTS.md](AGENTS.md)** — agent/CLI workflows: board ops, flash recovery, pointers to aggregator docs

## Features

- Dual-pane local ↔ board file transfer (upload, download, mkdir, new file, delete, rename, drag-and-drop)
- Edit board files in the editor (save writes back); optional SHA-256 verify
- Integrated ANSI REPL sharing the same serial session
- Connect detects MicroPython or CircuitPython, probes raw REPL, and sets RTC from the host
- Hard-reset auto-reconnect; Resume last device
- mpremote-backed ops: eval/exec/run, soft/hard reset, bootloader, mip, circup, df, …
- Agent CLI + local TCP RPC sharing the UI session
- **Firmware panel (MicroPython only):** Detect (esptool-first), Download (official catalog) or Build (local MicroPython tree), flash esp32 / rp2 / samd, partition autosize for esp32

## Requirements

- VS Code or Cursor (engine `^1.85.0`)
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
