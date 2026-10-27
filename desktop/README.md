# mpftp Desktop (preview)

mpftp as an app of its own: the browser interface's files, REPL and firmware
flashing in a desktop window, with its own Python inside, so there is nothing
to `pip install` first. It can also open a simulated board, the PyDevices
MicroPython WebAssembly build in a second window, which mpftp drives like any
other board.

Desktop is a preview. Windows is the only platform it builds for so far, the
installer isn't signed (so Windows warns the first time you run it), and it
isn't attached to releases yet. What comes next is in the
[roadmap](../ROADMAP.md).

## Try it

Every push builds the Windows installer. Open the
[Desktop workflow](https://github.com/PyDevices/mpftp/actions/workflows/desktop.yml),
pick a run on `main`, and download the `mpftp-desktop-windows-x64` artifact
from the bottom of its page (you need to be signed in to GitHub). Unzip it
and run `mpftp_<version>_x64-setup.exe`. It installs for your user only, so
there is no administrator prompt.

Logs go to `%LOCALAPPDATA%\com.pydevices.mpftp\logs`: `shell.log` for the
window and `host.log` for the Python side.

## How it fits together

The window is a [Tauri 2](https://v2.tauri.app/) app, `src-tauri/src/main.rs`.
It shows the same page `python -m mpftp` serves in a browser
(`cli/src/mpftp/webui/`, built from `ui/`), and starts the bundled Python
running `python -m mpftp.desktop_host` beside it. The page and the host talk
through the app over stdin and stdout, not a web server, so no port is open.
`ui/src/rpc.ts` picks that route inside the app and the WebSocket in a
browser, which is the only difference between the two.

The host is restarted if it dies, and on Windows it runs in a Job Object, so
closing the app never leaves a Python behind.

The simulated board is `emulator/`: a window running the WebAssembly runtime,
which the host reaches as an `emu://` port (`cli/src/mpftp/emu.py`).
`emulator/node-emu.mjs` runs the same board core under Node, for trying the
`emu://` path without the app.

Desktop's version is the repo's: `tauri.conf.json` reads it from
`package.json`, which `scripts/check_versions.py` keeps equal to the root
`VERSION` file with every other deliverable.

## Build it

The Windows installer cross-builds from Linux or WSL, the way CI builds it.
You need Node 20, [uv](https://docs.astral.sh/uv/), Rust with the
`x86_64-pc-windows-gnu` target, MinGW and NSIS:

```bash
sudo apt install gcc-mingw-w64-x86-64 nsis
rustup target add x86_64-pc-windows-gnu
```

The emulator's runtime comes from a
[PyDevices/workbench](https://github.com/PyDevices/workbench) checkout beside
this repo (`../workbench/assets/pydevices`), or from the folder in
`MPFTP_EMU_RUNTIME`. Then, from `desktop/`:

```bash
npm ci
npm run stage            # a Windows Python with mpftp's Windows wheels, in python-win/
npm run build:windows    # about four minutes
```

The installer lands in
`src-tauri/target/x86_64-pc-windows-gnu/release/bundle/nsis/`. Next to it in
`release/`, `mpftp.exe`, `WebView2Loader.dll` and `python/` run as they are
if you copy them to Windows together.
