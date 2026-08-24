# mpftp Developers Guide

## Repository layout

| Path | Role |
|------|------|
| `src/` | TypeScript extension host (bridge, panels, agent RPC) |
| `out/` | Compiled JS (shipped) |
| `python/sidecar.py` | Serial session (mpremote) |
| `python/firmware_engine.py` | Discover / build / flash / detect |
| `python/firmware_download.py` | Official firmware catalog |
| `media/` | Webview HTML/JS/CSS |
| `resources/templates/` | Workspace stub `micropython.cmake` / `manifest-micropython.py` |
| `docs/` | User and developer documentation |
| `docs/aggregator.md` | Workspace aggregators and user-module contract |
| `docs/agent-guide.md` | Agent/CLI playbook: boards, flash recovery; links to aggregator.md |
| `AGENTS.md` | Short entry-point for agents: build/lint/test commands, pointer to `docs/agent-guide.md` |
| `cli/src/mpftp/mcp.py` | `mpftp.mcp` — stdio MCP server (25 typed tools) over the same `RpcClient` the CLI uses |
| `integrations/` | Claude Code plugin + Codex config snippet wrapping `mpftp.mcp` |

Extension id: **`pydevices.mpftp`**.

## Architecture

```
┌─────────────────┐     JSON-lines      ┌──────────────────┐
│  Extension host │ ◄──────────────────► │  sidecar.py      │
│  (TS webviews)  │                      │  mpremote/serial │
└────────┬────────┘                      └──────────────────┘
         │ spawn (build/flash only)
         ▼
┌──────────────────┐
│ firmware_engine  │
└──────────────────┘
```

- **Sidecar** owns the board connection. File Transfer, REPL, and agent TCP RPC share it.
- **Firmware engine** is a separate process so long builds do not block the sidecar event loop.
- On WSL, serial + esp32 flash prefer Windows Python so `COM` ports are visible.

## Discovery contract

### MicroPython

1. `mpftp.micropythonPath` (hint)
2. `MP_DIR`
3. `~/micropython` (or `%USERPROFILE%\micropython`)
4. Firmware workspace + editor open folders (`micropython/` or the folder is the tree)
5. UI: Choose workspace… (open folders + Browse)

No personal path heuristics (no hardcoded forge layouts).

### Port dependency trees

Same rule for every SDK/repo a port needs:

1. Setting override (`mpftp.idfPath`, `mpftp.emsdkPath`, …)
2. Environment variable(s)
3. `<firmware-workspace>/<dirname>` (directory or symlink)
4. Else `needToolchain` → Locate… / Install instructions

Do not add well-known home directories that special-case one vendor SDK.

## Build and package

```bash
npm install
npm run compile          # tsc
npm run lint             # tsc --noEmit
npm run test:python      # unittest under python/tests
npm run package          # VSIX via @vscode/vsce

# WSL / Cursor remote extension host
./scripts/install-cursor-wsl.sh
# then: Developer: Reload Window
```

Native Linux serial (optional):

```bash
python3 -m venv .venv
.venv/bin/pip install mpremote
```

## Agent RPC

When the extension is active it listens on `127.0.0.1:7429` (see status / `~/.mpftp/`). The CLI (`python/mpftp_cli.py`) can share the UI session. Protocol matches sidecar JSON-lines plus firmware methods.

## Release notes

- Marketplace publisher: **pydevices**
- Repository: https://github.com/PyDevices/mpftp
- Releases are **git tags** (`vX.Y.Z`). See [publishing.md](publishing.md).

```bash
./scripts/next_release_version.sh --verbose
./scripts/publish_release_tag.sh --push   # creates vX.Y.Z; CI packages the VSIX
```

## Style

Prefer short comments that explain **why** a non-obvious constraint exists (discovery order, WSL Python choice, soft-reset vs corrupt filesystem). Avoid narrating obvious code.
