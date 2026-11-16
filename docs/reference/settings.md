# Settings, environment and files

One settings file serves every mcuctl front end. A value is taken from the first
of these that sets it: an explicit argument, the editor's setting, the
environment variable, `~/.mcuctl/config.json`, then the default.

## Editor settings

| Setting | Default | Purpose |
|---|---|---|
| `mcuctl.pythonPath` | empty | Python 3.9+ for the serial session. Empty on WSL means the Windows `python.exe` |
| `mcuctl.mpremotePath` | empty | Path to `mpremote`, for port listing fallback and diagnostics |
| `mcuctl.defaultBaud` | `115200` | Serial baud rate |
| `mcuctl.autoConnectDevice` | empty | A device to connect to on start (`COM4`, `/dev/ttyACM0`). Empty disables it |
| `mcuctl.verifyTransfers` | `true` | SHA-256 after each upload or download |
| `mcuctl.compileOnUpload` | `false` | Compile `.py` to `.mpy` with `mpy-cross` on upload (MicroPython only; `boot.py` and `main.py` stay source) |
| `mcuctl.autoReconnectAfterReset` | `true` | After a hard reset, wait for the same port and reconnect |
| `mcuctl.openEditorOnConnect` | `true` | Open File Transfer and the REPL when a board connects |
| `mcuctl.workspacePath` | empty | Firmware workspace: where mcuctl looks for omnipython-builds and MicroPython |
| `mcuctl.buildSystemPath` | empty | The omnipython-builds checkout to build with. Set, it wins over `config.json` and the workspace search |
| `mcuctl.micropythonPath` | empty | Override for the MicroPython tree |
| `mcuctl.idfPath`, `mcuctl.emsdkPath` | empty | Overrides for ESP-IDF and emsdk |
| `mcuctl.toolchainBins` | `[]` | Extra cross-toolchain `bin/` folders put on the build `PATH` |
| `mcuctl.buildPythonPath` | empty | Python for the build engine. On WSL it must be a native Linux `python3` |
| `mcuctl.esptoolCommand` | empty | The esptool command for flashing esp32, such as a Windows `python.exe` on WSL |
| `mcuctl.sharePasswords` | `ask` | Whether a WebREPL or Bluetooth password typed in VS Code is also written to `~/.mcuctl/webrepl-passwords.json`: `ask`, `always` or `never` |

## `~/.mcuctl/config.json` and environment variables

The file takes the same names without the `mcuctl.` prefix, plus these:

| Key | Environment variable | Purpose |
|---|---|---|
| `pythonPath` | `MCUCTL_PYTHON` | The Python for the session |
| `defaultBaud` | `MCUCTL_BAUD` | Serial baud rate |
| `autoConnectDevice` | `MCUCTL_DEVICE` | Device to connect to |
| `webreplPassword` | `MCUCTL_WEBREPL_PASSWORD` | Fallback WebREPL password |
| `blePassword` | `MCUCTL_BLE_PASSWORD` | Bluetooth password |
| `micropythonPath` | `MCUCTL_MICROPYTHON` | MicroPython tree |
| `workspacePath` | `MCUCTL_WORKSPACE` | Firmware workspace |
| `buildSystemPath` | `MCUCTL_BUILD_SYSTEM` | omnipython-builds checkout |
| `idfPath`, `emsdkPath` | `IDF_PATH`, `EMSDK` | SDK locations |
| `buildPythonPath` | `MCUCTL_BUILD_PYTHON` | Python for the build engine |
| `esptoolCommand` | `MCUCTL_ESPTOOL` | esptool command |
| `mpyExcludeFiles` | none | Files never compiled by `--mpy`. Default `["boot.py", "main.py"]` |
| `wifiBoards` | none | Boards mcuctl remembers from serial connects |

Other variables: `MCUCTL_RPC` (an editor session's address),
`MCUCTL_SESSION_ID` (a window's session name), `MCUCTL_PWA_PORT` (the browser
interface's port, default 8317).

## Files under `~/.mcuctl`

The tool used to be called mpftp and kept this folder as `~/.mpftp`. The first run that finds `~/.mpftp` and no `~/.mcuctl` moves it, so your saved devices carry over. Environment variables and editor settings that began `MPFTP_` or `mpftp.` now begin `MCUCTL_` and `mcuctl.`; the old names are ignored.

| Path | What it holds |
|---|---|
| `config.json` | The settings above |
| `webrepl-passwords.json` | Board passwords, plaintext, mode 0600 |
| `workspace-rpc.json` | Which editor session serves which workspace folder |
| `sessions/<id>.pid` | Each window's session claim |
| `activity.log` | NDJSON of connects, transfers, RPC calls and errors |
| `repl.log` | REPL I/O while a REPL is open |
| `holds/<key>/` | A `hold` session: `stream.bin` and `transcript.log` |
| `firmware-cache/` | Downloaded firmware images and the catalog pages |
| `package-cache/` | The package indexes `search` uses, refreshed every six hours |
