# Newcomer's guide to mpftp

mpftp is a board workbench for MicroPython and CircuitPython: a VS Code-compatible extension, a Python CLI, and a local browser app for file transfer and REPL work. It is published to TestPyPI as pydevices-mpftp.

## Choose a first entrypoint

For the editor experience, install the VSIX from the [latest release](https://github.com/PyDevices/mpftp/releases/latest), then run **mpftp: Connect to Board**.

For the local browser app:

```bash
pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple pydevices-mpftp
python -m mpftp
```

For terminal and agent-oriented work, the installed console command exposes operations such as `mpftp status`, `mpftp connect COM4`, `mpftp ls /`, and `mpftp exec 'print(42)'`.

The [user guide](user-guide.md) is the authoritative walkthrough for connection, transfer, packages, REPL, firmware workspaces, and recovery.

## Mental model

```text
VS Code extension UI ──┐
CLI / agent RPC ───────┼──> one extension sidecar ──> mpremote / board serial session
                       │
firmware build/flash ──┴──> separate firmware engine

python -m mpftp ──────────> separate loopback PWA and private board session
```

The sidecar is the serial owner. When an editor session is active, the CLI finds its workspace-scoped RPC registration (or an explicit MPFTP_RPC address) and shares that session instead of opening a second serial connection. Without one, the CLI starts a private sidecar and needs a device argument for board operations.

Firmware work is MicroPython-only and runs in a separate engine, so long builds do not hold the interactive serial session. On WSL, serial and ESP32 flash use Windows Python because it can reach COM ports.

## Repository map

| Path | Purpose |
|---|---|
| extension/src/ | TypeScript extension host, panels, terminal, bridge, and agent RPC server. |
| cli/src/mpftp/ | Published CLI package, sidecar, firmware engine, MCP server, and PWA launcher. |
| cli/tests/ | Python unit tests for the CLI and engines. |
| ui/ | PWA source; its built output is committed under cli/src/mpftp/webui/. |
| integrations/ | MCP and editor/agent integration configurations. |
| docs/user-guide.md | User workflows and troubleshooting. |
| docs/developers-guide.md | Architecture, discovery contract, packaging, and contribution details. |
| docs/agent-guide.md | Board-operation and recovery playbook for agents. |

## Important boundaries

MicroPython and CircuitPython differ in reset and package behavior. mpftp detects the interpreter and uses mip or circup accordingly; read the [reset and package boundary](user-guide.md#soft-reset-and-packages) before assuming equivalent behavior.

A firmware workspace is a MicroPython checkout (or a folder containing one) plus optional SDKs, with module repositories beside it. Downloading official firmware needs no checkout, while custom builds do. [Choosing what a build carries](firmware-modules.md) covers modules and presets.

The extension's vendored Python copy is staged from cli/src/mpftp/ for VSIX packaging. Edit the CLI source, not extension/python, then follow the developer packaging workflow.

## A safe first contribution

Start with a CLI unit test or a documentation change in the layer you are modifying. Run the extension commands from extension/; the repository's AGENTS.md lists compile, lint, package, and Python-test commands. For live boards, prefer the existing RPC connection rather than a second serial client.

