# mpftp Developers Guide

## Repository layout

| Path | Role |
|------|------|
| `extension/src/` | TypeScript extension host (bridge, panels, agent RPC, firmware/terminal/webview) |
| `extension/out/` | Compiled JS (shipped) |
| `extension/media/` | Webview HTML/JS/CSS |
| `extension/resources/templates/` | Workspace stub `micropython.cmake` / `manifest-micropython.py` |
| `extension/python/mpftp/` | Vendored copy of `cli/src/mpftp/`, staged in by `scripts/stage-vendored-python.sh` so `vsce` can package it (never edited directly) |
| `extension/package.json` | Extension manifest — `npm install` / `npm run compile` / `npm run package` all run **from `extension/`**, not the repo root |
| `cli/src/mpftp/` | The `pydevices-mpftp` Python package: `sidecar.py` (serial session), `firmware.py` (discover/build/flash/detect), `firmware_build.py` (runs `build_mp.py`), `firmware_download.py` (official firmware catalog), `pwa.py` (local web app), `cli.py`, `config.py`, `uf2.py` |
| `cli/src/mpftp/webui/` | Built PWA — **committed**, not gitignored: the TestPyPI release pipeline only runs `python -m build .`, with no Node step, so this has to already be current on `main`. CI's `ui` job rebuilds and `git diff --exit-code`s it on every push, so a stale commit fails CI rather than shipping silently |
| `cli/tests/` | Python test suite (`unittest`, run via `npm run test:python`) |
| `tools/` | Hardware CLI workflow test (`test_cli_workflows.py`); pass `--install-latest-firmware` to download+flash |
| `ui/` | The browser interface's source (TypeScript + esbuild, `@xterm/xterm`, CodeMirror 6). Its File Transfer panel is the extension's own `extension/media/ftp.html`, `ftp.js` and `ftp.css`, copied in by `npm run build`, which stages everything into `cli/src/mpftp/webui/`. `src/host.ts` gives `ftp.js` an `acquireVsCodeApi()` that talks to `cli/src/mpftp/panel.py`, the Python port of `FtpViewProvider`'s message handling, so a change to one host's behaviour belongs in the other too |
| `scripts/` | `mpftp` CLI launcher, release/version scripts, `install-cursor-wsl.sh`, `stage-vendored-python.sh` |
| `integrations/` | Claude Code plugin: a skill that teaches agents the CLI and points at `docs/agent-guide.md` |
| `docs/` | User and developer documentation |
| `docs/firmware-modules.md` | Building firmware with micropython-pydevices' `build_mp.py`: targets, modules, options |
| `docs/agent-guide.md` | Agent/CLI playbook: boards, flash recovery; links to firmware-modules.md |
| `AGENTS.md` | Short entry-point for agents: build/lint/test commands, pointer to `docs/agent-guide.md` |

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

### The build system (micropython-pydevices)

Builds run micropython-pydevices' `build_mp.py` (`cli/src/mpftp/firmware_build.py`):

1. `--build-system` (hint; the extension passes its `mpftp.buildSystemPath` setting here, see `extension/src/firmware/paths.ts`). A hint that isn't a checkout is a `BuildSystemError`, which `discover` reports as `buildSystemError`
2. `buildSystemPath` setting / `MPFTP_BUILD_SYSTEM`
3. Workspace roots (the CLI passes the current folder and its parents; the extension passes `mpftp.workspacePath` then the open folders), as the checkout or holding `micropython-pydevices/`
4. Beside the MicroPython tree (its parent, or a sibling `micropython-pydevices/`)
5. `~/micropython-pydevices`

What `firmware list` and `firmware modules` show is what that `build_mp.py`
offers: the engine loads it without running `main()` and calls the functions
its prompts use (`ports()`, `boards()`, `variants()`, `module_choices()`, and
the `cp_` ones for CircuitPython), in a child process. A build is one
`build_mp.py` run with no terminal on stdin, so a missing choice stops it
with an error rather than a prompt; the engine reports its `build_mp.py: ...`
line as the error and the first compiler error as `detail`.

### Port dependency trees

`build_mp.py` fetches ESP-IDF and emsdk at its locked versions. The engine
still checks for a cross-compiler a port needs on PATH (`arm-none-eabi-gcc`,
MinGW, ...) and returns `needToolchain` → Locate… / Install instructions when
one is missing.

Do not add well-known home directories that special-case one vendor SDK.

## Build and package

```bash
cd extension
npm install
npm run compile          # tsc
npm run lint             # tsc --noEmit
npm test                 # extension unit tests (node:test)
npm run test:python      # unittest under cli/tests
npm run package          # VSIX via @vscode/vsce

# WSL / Cursor remote extension host
cd ..
./scripts/install-cursor-wsl.sh
# then: Developer: Reload Window
```

Native Linux serial needs nothing extra: `npm run package` bundles mpremote
and pyserial (the hashed pins in `extension/vendor-requirements.txt`) into
`extension/python/_vendor`, and the launcher puts them on the sidecar's path.

## Official firmware downloads

`firmware_download.py` builds the download catalog the way micropython.org
builds its own download pages, so nothing in between has to be kept up to date.

**The board list** is `https://micropython.org/resources/boards/index.json`.
MicroPython's `tools/autobuild/build-downloads.py` writes it from every
`ports/<port>/boards/<BOARD>/board.json`, and the site's board pages are
rendered from it. Each entry has `id` (the board, `ESP32_GENERIC_S3`), `port`,
`mcu`, `vendor`, `product`, `variants` (name to description) and, for ESP
boards, `deploy_options.flash_offset`. The catalog keeps the ports mpftp can
flash from a download: esp32 (esptool) and rp2, samd, nrf and mimxrt (UF2).

**The files** have no listing of their own: `https://micropython.org/resources/firmware/`
answers 403. A board's page, `https://micropython.org/download/<BOARD>/`, links
every file it has, and the names follow the autobuild's contract
(`tools/autobuild/autobuild.sh`):

```
<BOARD>[-<VARIANT>]-<YYYYMMDD>-v<version>.<ext>
ESP32_GENERIC_P4-C6_WIFI-20260406-v1.28.0.bin
RPI_PICO-20260929-v1.30.0-preview.95.g5b5f06cb48.uf2
```

A variant name starts with a letter, so it can't be mistaken for the date. A
version with `-preview.` in it is a nightly; the server keeps the last few of
those and every release. ESP boards have `.bin` (the combined image, written
at the board's `flash_offset`), `.uf2`, `.app-bin`, `.elf`, `.map` and
`.meta.zip`; rp2 and samd have `.uf2`; stm32 has `.dfu` and `.hex`.

The index is cached for a day and each page for an hour in
`~/.mpftp/firmware-cache/`, with the downloaded images.

To check it still holds:

```bash
curl -s https://micropython.org/resources/boards/index.json | python -m json.tool | head
curl -s https://micropython.org/download/RPI_PICO/ | grep -o '/resources/firmware/[^"]*' | head
python -m mpftp.firmware download-list --board ESP32_GENERIC_S3 --force
```

The CLI's `download-*` commands and the browser's Firmware dialog
(`webflash.py`'s `firmware_catalog`, `firmware_board` and `firmware_fetch`)
share this code, and `firmware_fetch` downloads only a URL the board's page
listed.

## Agent RPC

When the extension is active it listens on `127.0.0.1:7429` (see status / `~/.mpftp/`). The CLI (`scripts/mpftp`, backed by `cli/src/mpftp/`) can share the UI session. Protocol matches sidecar JSON-lines plus firmware methods.

## Release notes

- Marketplace publisher: **pydevices**
- Repository: https://github.com/PyDevices/mpftp
- Releases are **git tags** (`vX.Y.Z`), cut by merging a release PR. See [publishing.md](publishing.md).

```bash
./scripts/next_release_version.sh --verbose
./scripts/publish_release_tag.sh --push   # creates vX.Y.Z; CI packages the VSIX
```

## Style

Prefer short comments that explain **why** a non-obvious constraint exists (discovery order, WSL Python choice, soft-reset vs corrupt filesystem). Avoid narrating obvious code.
