# Capture the console

`watch-repl` and `run --follow` show what the Python VM prints, but they
connect on the control port and drop the board to the REPL, so a board that
autostarts from `main.py` stops running under them. That is the wrong tool for
the ESP-IDF console: the panic or `Guru Meditation` backtrace from a native
crash, `ESP_LOG` output, and any `fprintf(stderr, ...)` from a C module. None of
those reach a Python-side log. They go straight to the console UART.

`monitor` is for that. It opens a port read-only, never enters the raw REPL,
and never toggles DTR or RTS, so the board isn't reset and the firmware keeps
running while you capture.

## See a board boot

Reset it and capture in one command, so nothing falls in the gap between the
two:

```bash
mpftp hard-reset -d PORT --monitor 20 --log-path boot.log
```

It resets the board, lets `main.py` or `code.py` run, waits up to 20 s for the
same port to come back (a native-USB board re-enumerates; a USB-UART bridge
such as the CH343 never goes away), then captures for 20 s. On a native TinyUSB
port it raises DTR, which that port treats as "a host is listening" and which
resets nothing; without it, CircuitPython and MicroPython's TinyUSB console send
nothing. A bridge or the ESP32 USB-Serial-JTAG keeps both lines low.

CircuitPython drops what it prints before the host has the port back, a few
seconds after the reset, so a `code.py` that fails in its first second shows
only the status line ending `Done`. The traceback is still there:

```bash
mpftp eval -d PORT "__import__('supervisor').get_previous_traceback()"
```

## Catch a crash while it happens

```bash
mpftp hard-reset -d PORT && mpftp disconnect -d PORT     # clean boot, release the port
mpftp monitor PORT --seconds 90 --log-path con.log &      # read-only capture, no reset
# ... trigger the crash ...
grep -iE 'guru|backtrace|panic|abort' con.log
```

Which port carries the console depends on the board's `CONFIG_ESP_CONSOLE_*`.
On a single-UART board it is the same port as the REPL, so capture only when
nothing else holds it (the board is on `main.py` and no `exec`, `run` or RPC
session is open). A dual-USB board may send it to the native USB CDC
(`role: cdc_debug` in `mpftp ports`). If `monitor` is silent during a crash, try
the other port. If you need to keep driving the board meanwhile, use
[Wi-Fi](connect-over-wifi.md) or [Bluetooth](connect-over-bluetooth.md) for the
commands and leave serial to `monitor`.

`monitor` replaces `debug-tee` for a bounded capture. `debug-tee` starts a
read-only tee and returns at once, and from the terminal the private session
(and the tee) dies with the command, so its log stays empty. `monitor` holds the
session open for `--seconds`, or until Ctrl-C.
