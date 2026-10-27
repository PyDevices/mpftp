# mpftp

mpftp is the board tool for MicroPython-compatible and
CircuitPython-compatible boards: it moves files, runs code, opens the REPL,
installs packages and flashes firmware, from VS Code, from a browser tab or from
a terminal.

```bash
pip install -i https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ pydevices-mpftp
```

That installs the `mpftp` command and the browser interface (`python -m mpftp`).
For VS Code, Cursor and other compatible editors, download `mpftp-*.vsix` from the
[latest release](https://github.com/PyDevices/mpftp/releases/latest) and run
**Extensions: Install from VSIX…**. [Install](docs/get-started/install.md) has
the details.

You don't need a board to try it. mpftp can hold a desktop MicroPython
interpreter open the same way it holds a board:

```bash
mpftp hold start --spawn micropython
mpftp hold ask -d micropython "import sys; print(sys.implementation.name, sys.implementation.version[:2])" --text
mpftp hold ask -d micropython "print(sum(range(10)))" --text
mpftp hold stop -d micropython
```

```text
micropython (1, 30)
45
```

`hold start` and `hold stop` also print their status as JSON. `-d micropython`
names the holder, in case a board is held too.

For a board, name its port instead: `mpftp ls -d /dev/ttyACM0 /`, or `COM3` on
Windows.

## Code signing policy

The Windows installer will be signed. Free code signing provided by
[SignPath.io](https://about.signpath.io), certificate by
[SignPath Foundation](https://signpath.org). SignPath signs Windows only, so
the Linux `.deb` is not signed. Who builds, reviews and approves each signed
release, and what the app sends over the network, is in the
[code signing policy](CODE_SIGNING_POLICY.md).

[Docs](https://mpftp.readthedocs.io) · [Examples](https://pydevices.github.io/pydevices-examples/gallery/) · [Roadmap](ROADMAP.md)

Made by [PyDevices](https://pydevices.com).
