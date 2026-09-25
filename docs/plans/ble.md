# mpftp over Bluetooth (bledev)

Status: the CLI and agents can use it, and so can VS Code and the PWA: their
Connect lists have a Bluetooth entry that looks for boards advertising
bledev's REPL and connects to the one you pick (mpftp#49).

## What you can do

Any board command takes `-d ble://NAME`, where `NAME` is what the board
advertises. You can use exec, eval, run, ls, put and get, the REPL, and
interrupt, and USB serial isn't involved at all:

```bash
export MPFTP_BLE_PASSWORD=...          # or blePassword in ~/.mpftp/config.json
mpftp exec -d ble://rack "print('hello')"
mpftp put -d ble://rack main.py /main.py
mpftp get -d ble://rack /data.bin ./data.bin
mpftp interrupt -d ble://rack
```

The board has to be serving. That takes pydevices' `bledev` package in
`/lib` and one line in `main.py`:

```python
import bledev.filetransfer
bledev.filetransfer.start(password="...", name="rack")   # the REPL and files
```

`bledev.repl.start(...)` serves the REPL alone, and mpftp works with that too,
only more slowly for files (see [the numbers](#why-files-dont-go-through-the-repl)).
Nothing starts either one at boot unless `main.py` does, because a board that
advertises a REPL should be something you chose.

## Where the password comes from

It's handled the same way as the WebREPL password
([mpftp#43](https://github.com/PyDevices/mpftp/issues/43)). mpftp looks in
three places, in order:

1. `~/.mpftp/webrepl-passwords.json` under `ble:NAME`. Save it with
   `mpftp wifi password ble://NAME`, which stores it as plaintext with mode
   0600.
2. `MPFTP_BLE_PASSWORD`, or `blePassword` in `~/.mpftp/config.json`.
3. The WebREPL password (`MPFTP_WEBREPL_PASSWORD` / `webreplPassword`),
   because `bledev.repl` uses `webrepl_cfg.PASS` when it isn't given one.

A BLE password is 4 to 64 characters. The link isn't encrypted, the same as
WebREPL, so anyone nearby with a sniffer can read it. Pairing is bledev's
later fix.

## How it works

Windows owns the laptop's radio, so the sidecar reaches the board through
bleak in the Windows Python, the same one it uses for COM ports. On WSL,
install it there: `python.exe -m pip install --user bleak`.

`mpftp/ble.py` dresses the link up as a pyserial port, the way `webrepl.py`
does for Wi-Fi, and hands it to mpremote's `SerialTransport`:

- **The REPL.** mpftp finds the board by name, connects, subscribes to the
  Nordic UART service, and logs in the way WebREPL does. It sends an empty
  line, the board answers `Password: `, and mpftp sends the password. A wrong
  password gets `Access denied` and a hang-up, and mpftp never retries it.
  Commands go in raw-paste mode, whose flow control keeps the board's input
  buffer from overflowing.
- **Files.** When the board also serves `bledev.filetransfer`, which is
  CircuitPython's BLE file-transfer protocol on service `0xFEBB`, `put` and
  `get` go through that instead. Logging in to the REPL unlocks it on the
  same connection. If the board answers a file command with an error, mpftp
  repeats the operation over the REPL, so you get mpremote's usual error
  message.
- **Checking.** `put` and `get` compare SHA-256 with the board afterwards,
  the same check they run over serial. The file-transfer protocol has no
  checksum of its own, so this check is the one that counts.
- **Connection parameters.** mpftp asks Windows for its throughput-optimized
  connection parameters. That roughly doubles every rate below.
- **Resets.** A soft reset turns Bluetooth off. `soft-reset` over BLE refuses
  and says why. `soft-reboot` runs `main.py`, which brings the link back if it
  starts bledev. `mount` is serial-only.

## Why files don't go through the REPL

These are 20 KB transfers between the laptop (Windows 11, bleak 3.0.2, Intel
radio) and the LilyGo T-Embed (ESP32-S3, MicroPython 1.29), one desk apart,
with the board's Wi-Fi off, on 2026-09-24. Each figure is the median of three
runs, and every run was checked byte for byte and against the board's
SHA-256. The script is [tools/ble_bench.py](../../tools/ble_bench.py).

| Path | Up | Down |
|---|---|---|
| File transfer, throughput parameters | 0.83 s (24.0 KB/s) | 0.30 s (67.5 KB/s) |
| File transfer, Windows' defaults | 1.37 s (14.6 KB/s) | 0.49 s (40.8 KB/s) |
| Raw REPL (mpremote, 256-byte chunks), throughput parameters | 15.6 s (1.3 KB/s) | 10.6 s (1.9 KB/s) |
| Raw REPL, 4 KB chunks, throughput parameters | 11.8 s (1.7 KB/s) | 6.3 s (3.2 KB/s) |
| Raw REPL (256-byte chunks), Windows' defaults | 43.6 s (0.5 KB/s) | 26.6 s (0.8 KB/s) |

File transfer is 19 times faster up and 35 times faster down than
mpremote's own raw-REPL transfers. Bigger chunks don't close the gap.
Raw-paste waits a round trip for every window of input the board grants,
and each exec's output crosses the same link as text. Plain raw REPL, which
is what the Wi-Fi transport uses, isn't safe here: `bledev.repl` keeps 4 KB
of unread input and drops anything beyond that.

The upload rate is set by the board, which writes flash between windows. A
larger window than `bledev.filetransfer`'s 4 KB doesn't help much, because
five round trips already cover 20 KB.

## The gate (2026-09-24)

This was run on the T-Embed over BLE, with USB serial used only to copy
`/lib/bledev` and `main.py` onto the board beforehand:

- `mpftp ls`, `put` (verified by SHA-256), `get` (compared byte for byte with
  `cmp`), and `exec` all pass. So does `interrupt` of a `while True` loop that
  `run` started (the board answered in 61 ms), and the REPL answers
  afterwards.
- [tools/ble_gate.py](../../tools/ble_gate.py) does the same in one sidecar
  session, the way the extension would. Connecting and logging in took 6 s,
  a 20 KB put took 1.1 s and a get 0.25 s. Both an interrupt and a later
  command stop a running loop.
- Planted faults: `MPFTP_BLE_PLANT=put` flips one bit in one uploaded chunk,
  and `put` exits 1 with a hash mismatch. `MPFTP_BLE_PLANT=get` flips one bit
  in a downloaded chunk, and `get` reports a hash mismatch.
- A wrong password gets "the board rejected the BLE REPL password". A name
  nothing is advertising gets "nothing is advertising as ... within 10 s".

## Not done

- The extension and the PWA don't list BLE boards. There's no discovery
  (`mpftp ble find`) and no remembered-board list.
- Only the laptop's Windows radio has been tried. bleak runs on Linux and
  macOS too, but the throughput request is WinRT-only and no other host has
  been tested.
- The board takes one client at a time. While mpftp holds the link, nothing
  else (a phone, Workbench) can connect.
