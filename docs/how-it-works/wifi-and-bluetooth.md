# Wi-Fi and Bluetooth transports

A wireless link is dressed up as a serial port, so everything mpremote and the
session already do (the raw REPL, exec, every file operation) works unchanged.
This page is why each link is built the way it is. To use them, see
[Connect over Wi-Fi](../guides/connect-over-wifi.md) and
[Connect over Bluetooth](../guides/connect-over-bluetooth.md).

## Wi-Fi (WebREPL)

`cli/src/mpftp/webrepl.py` holds a small WebSocket client written with the
standard library, `WebSocketSerial`. It has the pyserial surface that mpremote's
`SerialTransport` uses (`read`, `write`, `inWaiting`, `timeout`), and a subclass
of `SerialTransport` is handed that object instead of a port. The only
third-party packages are mpremote and pyserial, which the session already needed.

Out of the box that was far slower than serial. Four changes fixed it:

1. **Force the ACK the board waits for.** lwIP on the board writes a frame's
   2-byte header and its payload as two sends. Nagle holds the payload until the
   header is ACKed, and Windows delays that ACK by up to 200 ms. When the client
   sees a header without its payload, it sends an empty pong, which carries the
   ACK.
2. **Plain raw REPL, not raw-paste.** Raw-paste gives the host a 128-byte window,
   so every 128 bytes costs a Wi-Fi round trip. TCP already provides the flow
   control raw-paste exists for, so the command goes in one write.
3. **Uploads use WebREPL's binary PUT.** mpremote uploads by exec'ing
   `f.write(b'...')` in 256-byte chunks, which the board reads a byte at a time
   through `dupterm`, slower than serial. The PUT frames are read 512 bytes at a
   time. A PUT the board can't open raises inside its REPL input path and ends
   the session, so mpftp opens the file with an exec first. A failed flash write
   inside PUT is ignored on the board, so mpftp checks the size afterwards.
4. **Downloads are streamed, not printed.** Printed output is mirrored to the
   board's UART console and runs at its speed. An exec wraps
   `webrepl.client_s` in a second `websocket` object in binary mode and sends the
   file in 4 KB binary frames. WebREPL's own GET waits for an ACK every 256 bytes.
   If `webrepl.client_s` isn't there, mpftp falls back to mpremote's printed read.

Two things were tried and left out. `TCP_NODELAY` on the board's socket cut the
median exec time but starved a program that prints in a loop, and a user's
program matters more. WebREPL's GET is slow for the ACK reason above.

**Why a loop that never yields can't be interrupted.** The esp32 port services
its sockets only from `MICROPY_EVENT_POLL_HOOK`, which a loop that never sleeps
never reaches. Ctrl-C and new connections both wait on it. A busy board still
answers ping, because lwIP runs in its own task, so mpftp can tell "busy" from
"switched off" and says so in three places: when Ctrl-C gets no answer in 2.5 s,
when a connect to a board that logged in earlier this session stops answering,
and when a connect gets no answer from a host that still answers ping.

**Power save.** Before a transfer the session reads `WLAN(STA_IF).config('pm')`,
sets `PM_NONE`, and afterwards puts back exactly the value it read, in a
`finally`, so an error or cancel restores it too. It leaves power save alone if
`bluetooth.BLE().active()` (ESP-IDF needs modem sleep for Wi-Fi and Bluetooth
coexistence), and over serial. A batch of uploads is one change. If the
connection drops mid-transfer, power save stays off until the board resets, and
the next transfer in the session restores the saved value.

**Passwords on the wire.** The CLI and the browser interface look the password up
on your side and pass it to the session in the `connect` call, because a Windows
session spawned from WSL can't see your Linux environment or home folder. The
session keeps it in memory for reconnects, and the activity log redacts it.

**Discovery.** Every connect reads the board's uid, hostname and, when Wi-Fi is up,
its IP. A `NAME.local` lookup asks the OS resolver first and then sends its own
mDNS query (`cli/src/mpftp/mdns.py`, standard library only); the lookup runs in
the session, which on WSL is the Windows Python.

## Bluetooth (bledev)

Windows owns the computer's radio, so the session reaches the board through
bleak in the Windows Python, the same one it uses for `COM` ports.
`cli/src/mpftp/ble.py` dresses the link as a pyserial port the way `webrepl.py`
does, and hands it to `SerialTransport`:

- **The REPL.** mpftp finds the board by name, connects, subscribes to the Nordic
  UART service and logs in the way WebREPL does: it sends an empty line, the board
  answers `Password: `, and mpftp sends the password. A wrong password gets
  `Access denied` and a hang-up, and mpftp never retries it. Commands go in
  raw-paste mode, whose flow control keeps the board's 4 KB input buffer from
  overflowing; plain raw REPL, as Wi-Fi uses, isn't safe here.
- **Files.** When the board also serves `bledev.filetransfer`, which is
  CircuitPython's BLE file-transfer protocol on service `0xFEBB`, `put` and `get`
  use that instead, unlocked by logging in to the REPL on the same connection. If
  the board answers a file command with an error, mpftp repeats the operation over
  the REPL, so you get mpremote's usual error message.
- **Checking.** `put` and `get` compare SHA-256 with the board afterwards. The
  file-transfer protocol has no checksum of its own, so this is the check that
  counts.
- **Connection parameters.** mpftp asks Windows for its throughput-optimized
  connection parameters, which roughly doubles every rate. The request is
  WinRT-only.
- **Resets.** A soft reset turns Bluetooth off, so `soft-reset` refuses and
  `soft-reboot` runs `main.py`, which brings the link back if it starts `bledev`.

### Why files don't go through the REPL

Measured for 20 KB transfers between Windows and an ESP32-S3 on MicroPython 1.30
preview, with mpremote 1.29: median of three runs, each checked byte for byte and
against the board's SHA-256 (`tools/ble_bench.py` runs the benchmark):

| Path | Up | Down |
|---|---|---|
| File transfer, throughput parameters | 1.09 s (18.4 KB/s) | 0.38 s (52.6 KB/s) |
| File transfer, Windows defaults | 1.79 s (11.2 KB/s) | 1.11 s (18.0 KB/s) |
| Raw REPL, 256-byte chunks, throughput parameters | 15.6 s (1.3 KB/s) | 11.9 s (1.7 KB/s) |
| Raw REPL, 4 KB chunks, throughput parameters | 10.9 s (1.8 KB/s) | 6.8 s (2.9 KB/s) |
| Raw REPL, 256-byte chunks, Windows defaults | 51.0 s (0.4 KB/s) | 29.9 s (0.7 KB/s) |

File transfer is about 14 times faster up and 31 times faster down than
mpremote's own raw-REPL transfers, and bigger chunks don't close the gap:
raw-paste waits a round trip for every window of input the board grants, and each
exec's output crosses the same link as text. The upload rate is set by the board,
which writes flash between windows. `tools/ble_gate.py` runs an end-to-end check
of connect, put, get, exec and interrupt in one session.
