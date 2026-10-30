# AGENTS.md

- The extension is TypeScript in [`extension/`](extension/): `npm install`, then `npm run compile`, `npm run lint` and `npm test`.
- The CLI and firmware engine are Python in [`cli/`](cli/). Run its tests from the repo root with `PYTHONPATH=cli/src python -m unittest discover -s cli/tests` (about 80 seconds). Use a venv with the CLI's dependencies installed: the system `python3` fails 11 of them.
- `python3 tools/test_cli_workflows.py` drives attached boards, and skips flashing unless you pass `--install-latest-firmware`.
- Prefer the extension's TCP RPC over opening a second serial connection to the same board ([how commands find a session](docs/guides/drive-a-board-from-an-agent.md#how-commands-find-a-session)).
- On WSL, a `COMn` port and ESP32 flashing need Windows Python (`python.exe`); a `/dev/tty*` port (usbipd) runs in the Linux interpreter ([install](docs/get-started/install.md#vs-code-and-compatible-editors)).
- Board operations, CLI workflows, firmware build and flash, and troubleshooting: [drive a board from an agent](docs/guides/drive-a-board-from-an-agent.md).
