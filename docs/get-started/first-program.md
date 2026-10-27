# Your first session

You don't need a board to try mpftp. It can hold a desktop MicroPython
interpreter open the same way it holds a board, so this page uses one. Every
command works on a board once you name it with `-d`.

You need a `micropython` executable. If you don't have one, build the unix port
or download one from the MicroPython releases.

## Start a session and ask it something

```bash
mpftp hold start --spawn micropython
mpftp hold ask "import sys; print(sys.implementation.name, sys.implementation.version[:2])" --text
mpftp hold ask "print(sum(range(10)))" --text
mpftp hold stop
```

It prints:

```text
micropython (1, 30)
45
```

The version is whatever your interpreter reports. `hold start` opened the
interpreter and kept it running between commands. Each `hold ask` typed a line
at its prompt, waited for the next prompt and returned what came back, and
`--text` printed just that. `hold stop` let go.

Leave out `--text` and you get JSON with an `output` field and a `next` offset
you can pass to `hold read --since`.

## The same thing on a board

Boards are reached over USB serial, Wi-Fi or Bluetooth. List what is attached,
then name one:

```bash
mpftp ports
mpftp exec -d /dev/ttyACM0 "print(1 + 1)"
mpftp ls -d /dev/ttyACM0 /
```

On Windows a port is `COM3` and the like. `-d ws://HOST` reaches a board over
Wi-Fi and `-d ble://NAME` over Bluetooth.

Where to go next:

- [Connect to a board](../guides/connect-to-a-board.md) and [Transfer files](../guides/transfer-files.md)
- [Drive a running app](../guides/drive-a-running-app.md), which is what `hold` is for
- [Use the browser interface](../guides/use-the-browser-interface.md) if you'd rather click
