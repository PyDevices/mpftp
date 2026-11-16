# Connect over Bluetooth

A board running pydevices' `bledev` can be reached over Bluetooth LE with no
cable to your computer. Any board command takes `-d ble://NAME`, where `NAME`
is the name the board advertises:

```bash
export MCUCTL_BLE_PASSWORD=...          # or blePassword in ~/.mcuctl/config.json
mcuctl exec -d ble://rack "print('hello')"
mcuctl put -d ble://rack main.py /main.py
mcuctl get -d ble://rack /data.bin ./data.bin
mcuctl interrupt -d ble://rack
```

You can use exec, eval, run, ls, put, get, the REPL and interrupt. In the
editor and the browser interface, pick **Look for Bluetooth boards…** in the
Connect list. It lists what is advertising `bledev`'s REPL nearby, strongest
signal first; pick one or type the name.

## Prepare the board

Do this over serial. Copy `lib/bledev` to `/lib/bledev` on the board, then call
it from `main.py`:

```python
import bledev.filetransfer
bledev.filetransfer.start(password="...", name="rack")   # the REPL and files
```

`bledev.repl.start(...)` serves the REPL alone, and mcuctl works with that too,
with slower files. Nothing starts either at boot unless `main.py` does.

A serial connect soft-resets the board, and a soft reset turns Bluetooth off.
After setup, `hard-reset` the board and stay off its serial port.
`mcuctl hard-reset --monitor 15` does the same and shows you the boot, read-only,
so you see `main.py` start (or its traceback).

## Passwords

mcuctl looks in three places, in order:

1. `~/.mcuctl/webrepl-passwords.json` under `ble:NAME`. `mcuctl wifi password
   ble://NAME` saves it there.
2. `MCUCTL_BLE_PASSWORD`, or `blePassword` in `~/.mcuctl/config.json`.
3. The WebREPL password, since `bledev.repl` uses `webrepl_cfg.PASS` when it
   isn't given one.

A BLE password is 4 to 64 characters. VS Code and the browser interface ask on
first connect and keep it for that board.

## Pairing

A board started with `pairing="justworks"` is paired by mcuctl itself. A board
started with `pairing="passkey"` needs your computer paired once: run
`python -m bledev.bleak pair NAME` and type the passkey the board shows (or use
Windows Settings, Add device). After that mcuctl uses the bond, with no password
if the board has none. If the board lost its keys (a chip erase), the link
drops at the first write; run `python -m bledev.bleak unpair NAME` and pair
again.

## Limits

- `soft-reset` refuses over Bluetooth, since it would switch Bluetooth off and
  skip the `main.py` that starts it. Use `soft-reboot` or `hard-reset`. `mount`
  is serial-only.
- A board takes one client at a time, so while mcuctl holds the link nothing
  else can connect. Give the board a name nobody else advertises.
- The link isn't encrypted unless you pair, so anyone nearby with a sniffer
  can read an unpaired password.
- `bleak` has to be installed. `pip install mcuctl` brings it; on WSL
  the radio belongs to Windows, so install it for the Windows Python the
  sidecar uses: `python.exe -m pip install --user bleak`.

How it works and how fast it is: [Wi-Fi and Bluetooth transports](../how-it-works/wifi-and-bluetooth.md).
