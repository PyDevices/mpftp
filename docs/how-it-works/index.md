# How mpftp works

mpftp is a board tool for MicroPython and CircuitPython: a VS Code-compatible
extension, a Python command line, and a local browser app for file transfer and
REPL work. Those three are different faces on the same parts. This page is the
model; the guides say how to use it.

## One owner for the serial port

```text
VS Code extension UI ──┐
CLI / agent RPC ───────┼──> one extension sidecar ──> mpremote / board serial session
                       │
firmware build/flash ──┴──> separate firmware engine

python -m mpftp ──────────> separate loopback browser app and private board session
```

A long-lived Python **sidecar** owns the serial link, built on mpremote, and
speaks JSON lines to the extension. File Transfer, the REPL and the terminal
commands all go through it, which is why they don't fight over the port. When an
editor session is active, the CLI finds its session (through
`~/.mpftp/workspace-rpc.json`, or an explicit `MPFTP_RPC` address) and shares
it instead of opening a second connection. Without one, the CLI starts a private
sidecar and needs `-d DEVICE` for board operations. The extension's loopback
server binds an ephemeral port, so there is no fixed port to remember.

`python -m mpftp` is a separate thing: a loopback web server with its own
private board session. That is why you connect a board in the editor or in the
browser interface, not both.

## Firmware work runs apart

Builds and flashes run in a separate engine process, so a long compile never
holds the interactive serial session. A build is one run of micropython-pydevices'
`build_mp.py`; the engine reports its log and its error. On WSL, serial and ESP32
flashing use the Windows Python, because only it can open `COM` ports, while
builds use a native Linux Python.

## Two interpreters, one interface

mpftp detects the interpreter on connect (`sys.implementation.name`) and adjusts.
Resets, package installs and file routing differ between MicroPython and
CircuitPython: see [Run code and use the REPL](../guides/run-code-and-use-the-repl.md#interrupt-and-reset),
[Install packages](../guides/install-packages.md) and
[Work with CircuitPython](../guides/work-with-circuitpython.md).

## Where to read next

- [Finding the tools you need](finding-tools.md): how mpftp locates MicroPython, the build system and the SDKs.
- [Official firmware downloads](firmware-downloads.md): where the catalog comes from.
- [Wi-Fi and Bluetooth transports](wifi-and-bluetooth.md): how a wireless link is dressed as a serial port.
- Contributing to mpftp: [CONTRIBUTING.md](https://github.com/PyDevices/mpftp/blob/main/CONTRIBUTING.md).
