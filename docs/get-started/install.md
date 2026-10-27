# Install

mpftp comes in three forms. They share one settings file and the same
commands underneath, so pick the one that suits how you work.

## The command line and browser interface

```bash
pip install -i https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ pydevices-mpftp
```

This installs the `mpftp` command and the browser interface, with
[mpremote](https://pypi.org/project/mpremote/), pyserial, circup, esptool and
bleak. It needs Python 3.9 or newer. Check it:

```bash
mpftp --version
```

`python -m mpftp` with no arguments opens the browser interface;
[Use the browser interface](../guides/use-the-browser-interface.md) covers it.
`python -m mpftp` followed by a command is the same as running `mpftp`.

## VS Code and compatible editors

The extension works in VS Code, Cursor, VSCodium and Google Antigravity (engine
`^1.85.0`). It is not on the VS Code Marketplace or Open VSX. Download the
`.vsix` from the [latest GitHub release](https://github.com/PyDevices/mpftp/releases/latest),
then run **Extensions: Install from VSIX…** and choose `mpftp-*.vsix`. Reload
the window if it asks, then click the **mpftp** status bar item or run
**mpftp: Connect to Board**.

The extension brings its own mpremote and pyserial, so serial, WebREPL, file
transfer and the REPL need no pip install. Two features use libraries it does
not bundle, which must be installed on the Python the extension uses
(`mpftp.pythonPath`):

- **CircuitPython packages** need [circup](https://pypi.org/project/circup/):
  `pip install circup`.
- **Bluetooth** (`ble://`) needs [bleak](https://pypi.org/project/bleak/):
  `pip install bleak`.

Which Python that is depends on where you run:

| Where you run | Python the extension uses |
|---|---|
| Windows | The Windows Python |
| WSL | A Windows Python, because only it can open `COM` ports. Install with `python.exe -m pip install circup bleak` |
| Linux | The system `python3`, or the path in `mpftp.pythonPath` |

## The desktop app

A Windows app with its own Python inside and a simulated board in a second
window. It is a preview; [the desktop README](https://github.com/PyDevices/mpftp/blob/main/desktop/README.md)
says how to get it.

## Build firmware

Downloading and flashing an official image needs nothing more. Building your
own needs a [micropython-pydevices](https://github.com/PyDevices/micropython-pydevices)
checkout; [Build firmware](../guides/build-firmware.md) says how to get one.
