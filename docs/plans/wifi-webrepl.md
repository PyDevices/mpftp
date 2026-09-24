# mpftp over Wi-Fi (WebREPL)

Status: phase 1 done (transport, CLI and sidecar wiring, tests). Phase 2 is the
VS Code and PWA UI, and it waits on the decisions at the end of this page.

## What you can do

Give any board command a WebREPL address instead of a serial port:

```bash
export MPFTP_WEBREPL_PASSWORD=...      # or "webreplPassword" in ~/.mpftp/config.json
mpftp exec -d ws://192.168.1.147:8266 "print('hello')"
mpftp put -r -d ws://192.168.1.147 lib /lib
mpftp get -r -d ws://192.168.1.147 /lib ./lib-copy
mpftp soft-reset -d ws://192.168.1.147
```

The port defaults to 8266. Serial devices behave exactly as before; nothing in
the serial path changed. The board needs Wi-Fi up and `webrepl.start()` running.
To survive a reset, both have to happen in `boot.py`:

```python
import wifi, webrepl
wifi.connect_from_secrets()
webrepl.start(password="...")   # or webrepl_cfg.py, as upstream's webrepl_setup writes
```

## Password handling

The password comes from `MPFTP_WEBREPL_PASSWORD`, or `webreplPassword` in
`~/.mpftp/config.json`. It never goes in the address: `ws://:pw@host` is
refused. The CLI reads it and passes it to the sidecar in the `connect` call,
because a Windows sidecar spawned from WSL can't see your Linux environment or
home directory. The sidecar keeps it in memory for reconnects. The extension's
activity log redacts it. WebREPL caps passwords at 9 characters, and mpftp
refuses a longer one rather than let the board silently cut it short.

WebREPL has no TLS. The password and everything after it cross your network in
the clear, so use it on a network you trust.

## The design, and why

[`cli/src/mpftp/webrepl.py`](../../cli/src/mpftp/webrepl.py) has a small
WebSocket client written with only the standard library, `WebSocketSerial`.
It has the pyserial surface that mpremote's `SerialTransport` uses (`read`,
`write`, `inWaiting`, `timeout`). A subclass of `SerialTransport` is handed that
object instead of a COM port, so raw REPL, exec, and every `fs_*` operation
mpremote and the sidecar already use work unchanged. The only third-party
packages are mpremote and pyserial, which the sidecar already needed.

Out of the box, that was far slower than serial. Four measured changes fixed
it:

1. **Force the ACK the board waits for.** lwIP on the board writes a frame's
   2-byte header and its payload as two sends. Nagle holds the payload until the
   header is ACKed, and Windows delays that ACK by up to 200 ms. When the client
   sees a header without its payload, it sends an empty pong, which carries the
   ACK. Every reply stalled for 200 ms before that.
2. **Plain raw REPL, not raw-paste.** Raw-paste gives the host a 128-byte
   window, so every 128 bytes costs a Wi-Fi round trip: a 4 KB script took
   5–6 s. TCP already provides the flow control raw-paste exists for, so the
   command goes in one write.
3. **Uploads use WebREPL's binary PUT.** mpremote uploads by exec'ing
   `f.write(b'...')` in 256-byte chunks. The board reads REPL input a byte at
   a time through `dupterm`, at about 3 KB/s, which came to 0.2–0.9 KB/s. That
   was slower than serial. The PUT frames are read 512 bytes at a time instead.
   A PUT the board can't open raises inside its REPL input path and ends the
   session, so mpftp opens the file with an exec first. A failed flash write
   inside PUT is ignored on the board (`assert(0)` is compiled out), so mpftp
   checks the size afterwards.
4. **Downloads are streamed, not printed.** Printed output is mirrored to the
   board's UART console and runs at its speed (1–3 KB/s). An exec wraps
   `webrepl.client_s` in a second `websocket` object in binary mode, and the
   file goes out in 4 KB binary frames. WebREPL's own GET waits for an ACK
   every 256 bytes and managed 2.7–3.4 KB/s. If `webrepl.client_s` isn't
   there, mpftp falls back to mpremote's printed read.

Two things were tried and left out:

- **TCP_NODELAY on the board's socket.** It took the median exec from 96 to
  22 ms. It also starved a program that prints in a loop: under 1 KB arrived
  in 8 s, against 81 KB without it. A user's program matters more.
- **WebREPL GET**, for the ACK-per-chunk reason above.

## Measured on the P4 (Waveshare ESP32-P4 panel, C6 over ESP-Hosted)

These numbers come from Windows `python.exe`, the way the sidecar runs. RSSI was
-67 to -80.

| | serial COM4 | Wi-Fi |
|---|---|---|
| `exec` output | identical | identical |
| median exec round trip | 20 ms | 20–95 ms (varies with the link) |
| one 128 KB file, put / get | 2.7 / 2.8 KB/s | 45–51 / 48–204 KB/s |
| 20 files, 240 KB incl. a 150 KB binary, `put -r` / `get -r` | 2.7 / 2.8 KB/s | 7.0 / 9.2 KB/s (power save off), 3.9 / 6.4 KB/s (power save on) |

The directory round trip checks each upload's SHA-256 on the board, and all 20
downloaded files matched their sources. With 20 files, the per-file round trips
cost more than moving the bytes does, so the directory figure tracks Wi-Fi
latency. The board's default Wi-Fi power save (`pm=1`) put the average ping at
170–180 ms. `pm=PM_NONE` brought it to 39–51 ms.

Interrupt and reset:

- **Ctrl-C** stops a loop that sleeps or waits in 0.06–0.22 s.
- **A loop that never sleeps can't be interrupted over WebREPL.** That covers a
  bare `while True: n += 1`, and a loop that only prints. It also stops the board
  answering a new WebREPL connection. The ESP32 port polls sockets only in
  `MICROPY_EVENT_POLL_HOOK`, which a busy loop never reaches, so this is a
  firmware limit. Serial or a hard reset still works.
- **`soft-reset`** does a raw soft reset, which frees WebREPL's socket, then
  reconnects. That took 7.2 s including `boot.py` restarting WebREPL. The state
  is gone and `main.py` is skipped.
- **`soft-reboot`** leaves `main.py` running and ends the session. `resume`
  reconnects, and reconnecting interrupts `main.py`.
- **Connect doesn't soft-reset over Wi-Fi.** It interrupts only, so the VM keeps
  its state. A reset would kill the connection being made.

Failures:

- A wrong password fails in 1.2 s with "the board rejected the WebREPL
  password", and it isn't retried.
- A missing password fails at once.
- An address with no board on it fails in 5 s.
- A second client gets "Another WebREPL client may be connected".

## What phase 2 needs decided

- **Where the password lives in the UI.** VS Code SecretStorage or the
  settings file? One password for every board, or one per address?
- **Whether mpftp sets up the board.** An "enable Wi-Fi access" action could
  write the `boot.py` lines above, or `webrepl_cfg.py`. That changes the
  user's boot file. Without it, WebREPL doesn't come back after any reset.
- **Wi-Fi power save.** Turning it off on connect roughly halves directory
  transfer times. That's a change to the board's radio state that outlives
  the session.
- **Finding the board.** Phase 1 needs a typed IP. The options are mDNS, a
  scan, or remembering the address from a serial session (`wlan.ifconfig()`).
- **Loops that can't be interrupted.** You can accept that as a documented
  limit, or fix it in the firmware by polling socket events from the VM's
  pending-work check. That fix is an overlay patch, or an upstream PR.
- **The UI's live REPL.** The extension's REPL reader works on this transport
  unchanged, but it hasn't been exercised over Wi-Fi yet. `mount` hasn't been
  tried at all.
