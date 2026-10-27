# Connect to a board

mpftp reaches a board over USB serial, Wi-Fi ([Connect over Wi-Fi](connect-over-wifi.md))
or Bluetooth ([Connect over Bluetooth](connect-over-bluetooth.md)). Connecting
works the same way in the editor, the browser interface and the terminal.

## In an editor

Click the **mpftp** status bar item or run **mpftp: Connect to Board**, then
pick a port (`COM3`, `/dev/ttyACM0` and so on). **mpftp: Resume Last Device**
reconnects to the previous one. For a CircuitPython board, prefer its REPL
serial interface: the data interface of a second CDC port is filtered out.

## From the terminal

```bash
mpftp ports                  # what is attached
mpftp connect /dev/ttyACM0
mpftp status                 # connected? which interpreter?
mpftp resume                 # reconnect to the last device
```

Connect interrupts any running program and enters the raw REPL. It reads
`sys.implementation.name` and reports `interpreter` as `micropython` or
`circuitpython`, and every later command adjusts to that. The two differ in
what a reset does and in how packages install; see
[Work with CircuitPython](work-with-circuitpython.md).

You can also skip `connect` and pass `-d PORT` to any command. With no
editor session running, the command opens a private session for itself.

## The clock

Connect sets the board's clock to the host's UTC only when the board's clock
is unset (a year before 2024). A clock that NTP or anything else has set is
left alone. `mpftp rtc --set` always sets it, in UTC on both interpreters, and
`mpftp rtc` reads it back in `machine.RTC().datetime()` order. mpremote's
own `rtc --set` writes local time, so the two tools disagree by your UTC
offset.

## One program owns the port

A serial port can have one owner. If another tool holds it (a serial monitor,
Thonny, another mpftp window), connect fails with a busy error; close the other
tool or disconnect there. While an editor is connected, the terminal commands
share its session instead of opening a second connection, which is why they
find it first: [Drive a board from an agent](drive-a-board-from-an-agent.md#how-commands-find-a-session)
has the details.

On a board whose program never returns control, Windows can report the port
as busy or locked. [Troubleshooting](troubleshooting.md) covers what to try.
