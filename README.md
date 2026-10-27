# mpftp

mpftp is the board workbench for MicroPython-compatible and
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
mpftp hold ask "import sys; print(sys.implementation.name, sys.implementation.version[:2])" --text
mpftp hold ask "print(sum(range(10)))" --text
mpftp hold stop
```

```text
micropython (1, 30)
45
```

For a board, name its port instead: `mpftp ls -d /dev/ttyACM0 /`, or `COM3` on
Windows.

[Docs](https://mpftp.readthedocs.io) · [Examples](https://mpftp.readthedocs.io/en/latest/get-started/first-program.html) · [Roadmap](ROADMAP.md)

Made by [PyDevices](https://pydevices.com).
