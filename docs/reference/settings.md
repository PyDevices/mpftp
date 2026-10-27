# Settings, environment and files

One settings file serves every mpftp front end. A value is taken from the first
of these that sets it: an explicit argument, the editor's setting, the
environment variable, `~/.mpftp/config.json`, then the default.

## Editor settings

| Setting | Default | Purpose |
|---|---|---|
| `mpftp.pythonPath` | empty | Python 3.9+ for the serial session. Empty on WSL means the Windows `python.exe` |
| `mpftp.mpremotePath` | empty | Path to `mpremote`, for port listing fallback and diagnostics |
| `mpftp.defaultBaud` | `115200` | Serial baud rate |
| `mpftp.autoConnectDevice` | empty | A device to connect to on start (`COM4`, `/dev/ttyACM0`). Empty disables it |
| `mpftp.verifyTransfers` | `true` | SHA-256 after each upload or download |
| `mpftp.compileOnUpload` | `false` | Compile `.py` to `.mpy` with `mpy-cross` on upload (MicroPython only; `boot.py` and `main.py` stay source) |
| `mpftp.autoReconnectAfterReset` | `true` | After a hard reset, wait for the same port and reconnect |
| `mpftp.openEditorOnConnect` | `true` | Open File Transfer and the REPL when a board connects |
| `mpftp.workspacePath` | empty | Firmware workspace: where mpftp looks for micropython-pydevices and MicroPython |
| `mpftp.buildSystemPath` | empty | The micropython-pydevices checkout to build with. Set, it wins over `config.json` and the workspace search |
| `mpftp.micropythonPath` | empty | Override for the MicroPython tree |
| `mpftp.idfPath`, `mpftp.emsdkPath` | empty | Overrides for ESP-IDF and emsdk |
| `mpftp.toolchainBins` | `[]` | Extra cross-toolchain `bin/` folders put on the build `PATH` |
| `mpftp.buildPythonPath` | empty | Python for the build engine. On WSL it must be a native Linux `python3` |
| `mpftp.esptoolCommand` | empty | The esptool command for flashing esp32, such as a Windows `python.exe` on WSL |
| `mpftp.sharePasswords` | `ask` | Whether a WebREPL or Bluetooth password typed in VS Code is also written to `~/.mpftp/webrepl-passwords.json`: `ask`, `always` or `never` |

## `~/.mpftp/config.json` and environment variables

The file takes the same names without the `mpftp.` prefix, plus these:

| Key | Environment variable | Purpose |
|---|---|---|
| `pythonPath` | `MPFTP_PYTHON` | The Python for the session |
| `defaultBaud` | `MPFTP_BAUD` | Serial baud rate |
| `autoConnectDevice` | `MPFTP_DEVICE` | Device to connect to |
| `webreplPassword` | `MPFTP_WEBREPL_PASSWORD` | Fallback WebREPL password |
| `blePassword` | `MPFTP_BLE_PASSWORD` | Bluetooth password |
| `micropythonPath` | `MPFTP_MICROPYTHON` | MicroPython tree |
| `workspacePath` | `MPFTP_WORKSPACE` | Firmware workspace |
| `buildSystemPath` | `MPFTP_BUILD_SYSTEM` | micropython-pydevices checkout |
| `idfPath`, `emsdkPath` | `IDF_PATH`, `EMSDK` | SDK locations |
| `buildPythonPath` | `MPFTP_BUILD_PYTHON` | Python for the build engine |
| `esptoolCommand` | `MPFTP_ESPTOOL` | esptool command |
| `mpyExcludeFiles` | none | Files never compiled by `--mpy`. Default `["boot.py", "main.py"]` |
| `wifiBoards` | none | Boards mpftp remembers from serial connects |

Other variables: `MPFTP_RPC` (an editor session's address),
`MPFTP_SESSION_ID` (a window's session name), `MPFTP_PWA_PORT` (the browser
interface's port, default 8317).

## Files under `~/.mpftp`

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
