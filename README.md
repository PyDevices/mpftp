# mcuctl

mcuctl is the board tool for MicroPython-compatible and
CircuitPython-compatible boards: it moves files, runs code, opens the REPL,
installs packages and flashes firmware, from VS Code, from a browser tab or from
a terminal.

```bash
pip install mcuctl
```

That installs the `mcuctl` command and the browser interface (`python -m mcuctl`).
For VS Code, Cursor and other compatible editors, download `mcuctl-*.vsix` from the
[latest release](https://github.com/PyDevices/mcuctl/releases/latest) and run
**Extensions: Install from VSIX…**. [Install](docs/get-started/install.md) has
the details.

You don't need a board to try it. mcuctl can hold a desktop MicroPython
interpreter open the same way it holds a board:

```bash
mcuctl hold start --spawn micropython
mcuctl hold ask -d micropython "import sys; print(sys.implementation.name, sys.implementation.version[:2])" --text
mcuctl hold ask -d micropython "print(sum(range(10)))" --text
mcuctl hold stop -d micropython
```

```text
micropython (1, 30)
45
```

`hold start` and `hold stop` also print their status as JSON. `-d micropython`
names the holder, in case a board is held too.

For a board, name its port instead: `mcuctl ls -d /dev/ttyACM0 /`, or `COM3` on
Windows.

## Code signing policy

The Windows installer will be signed. Free code signing provided by
[SignPath.io](https://about.signpath.io), certificate by
[SignPath Foundation](https://signpath.org). SignPath signs Windows only, so
the Linux `.deb` is not signed. Who builds, reviews and approves each signed
release, and what the app sends over the network, is in the
[code signing policy](CODE_SIGNING_POLICY.md).

[Docs](https://github.com/PyDevices/mcuctl/blob/main/docs/index.md) · [Examples](https://gallery.omnipython.org/) · [Roadmap](ROADMAP.md)

Made by [PyDevices](https://pydevices.com).
