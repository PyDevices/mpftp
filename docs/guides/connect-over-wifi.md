# Connect over Wi-Fi

Once a board is set up, you can reach it without a cable. Every board command
takes `-d ws://HOST[:8266]`, and the REPL, file transfer, run, exec and resets
all work as they do over serial. The editor's Connect list and the browser
interface list the board under **Wi-Fi** by name.

## Set a board up

Connect over USB once and run **mcuctl: Enable Wi-Fi Access** (the browser
interface's **Wi-Fi access...** button, or `mcuctl wifi enable -d PORT`). You
choose a password. mcuctl shows you the exact change to the board's files and
writes nothing until you say yes, and refuses to write if one of those files
changed after you looked. It works with stock firmware: nothing beyond what
MicroPython or CircuitPython ship has to be on the board.

Either way the Wi-Fi name and password come from a file on the board, never
from your computer: mcuctl only checks it is there, and the board reads it.
Copy yours on first (`mcuctl put secrets.py /secrets.py`).

### MicroPython

The board needs a `secrets.py` that defines `WIFI_SSID` and `WIFI_PASSWORD`,
and the `webrepl` module. **Enable** then writes two files:

- `/webrepl_cfg.py` holds `PASS = "..."`, the line upstream's `webrepl_setup`
  writes. WebREPL reads its password from there, so it isn't in `boot.py`.
- `/boot.py` gets a block between `# >>> mcuctl wifi-access >>>` and
  `# <<< mcuctl wifi-access <<<` at the top. It joins your network with the
  built-in `network.WLAN` and `secrets.py`, waits up to 20 seconds, then calls
  `webrepl.start()`. If it can't join it prints a line and boot carries on.

A `webrepl_cfg.py` that is already there is shown in the change and kept as
`/webrepl_cfg.py.mcuctl-orig`. **Disable Wi-Fi access** (`mcuctl wifi disable`)
removes the block, gives back `boot.py` byte for byte (or deletes it if mcuctl
created it), and puts the old `webrepl_cfg.py` back or deletes the one mcuctl
wrote. `mcuctl wifi disable` also forgets the password mcuctl saved for the board,
since it no longer matches.

If the board has a `wifi_manager` module, the block tries
`wifi_manager.connect(setup=False)` first. It reads the same `secrets.py`,
remembers networks and joins faster; the plain join is the fallback.

### CircuitPython

The board needs a `secrets.py` with `WIFI_SSID` and `WIFI_PASSWORD` (or
Adafruit's older `secrets = {"ssid": ..., "password": ...}`), unless
`settings.toml` already has `CIRCUITPY_WIFI_SSID` and
`CIRCUITPY_WIFI_PASSWORD`. **Enable** puts the settings the file lacks at the top
of `/settings.toml`: `CIRCUITPY_WIFI_SSID`, `CIRCUITPY_WIFI_PASSWORD` and
`CIRCUITPY_WEB_API_PASSWORD`, which is your password. Code running on the board
copies the Wi-Fi name and password from `secrets.py`, so mcuctl never holds them,
and the change you confirm shows them as `*****`.

CircuitPython has no WebREPL, so `webrepl_cfg.py` is MicroPython-only and mcuctl
writes none here. Reset the board afterwards: CircuitPython reads `settings.toml` at boot, then
joins and serves its [web workflow](https://docs.circuitpython.org/en/latest/docs/workflows.html#web)
at `http://BOARD-IP/`. **Disable** removes the block and gives back
`settings.toml` byte for byte. mcuctl's own Wi-Fi connection (below) is
WebREPL's, so you use a CircuitPython board through its web workflow, for
example `mcuctl circup --host`.

While your computer has the board's `CIRCUITPY` drive mounted, the board can't
write its own files. Then mcuctl writes `settings.toml` through the drive, which
means it can't use the board's `secrets.py`: `mcuctl wifi enable` asks you to type
the Wi-Fi name and password (nothing is printed or logged). The browser and
editor stop with an error instead; turn the board's USB drive off with
`storage.disable_usb_drive()` in `boot.py` and try again, and the board copies
from its own `secrets.py` as above.

## Connect

```bash
mcuctl wifi boards                          # boards mcuctl has seen with Wi-Fi up
mcuctl wifi find mpy-board                # NAME.local by mDNS, best effort
mcuctl exec -d ws://BOARD-IP "print('hello')"
mcuctl put -r -d ws://BOARD-IP lib /lib
mcuctl get -r -d ws://BOARD-IP /lib ./lib-copy
```

A serial connect to a board with Wi-Fi up records it under `wifiBoards` in
`~/.mcuctl/config.json`, and that is where the Connect lists get their names.
You can also type an address, or a `NAME.local` that mcuctl looks up by mDNS.
A board keeps every address you reach it at, and its password works at each of
them, including a new name that resolves to an address it already has.

## Passwords

WebREPL keeps at most 9 characters, and a password mcuctl writes to a board is
4 to 9. Each board has its own, kept in one of these places:

- The CLI and the browser interface keep them in
  `~/.mcuctl/webrepl-passwords.json`, plaintext, readable only by you.
  `mcuctl wifi password BOARD` saves one. `MCUCTL_WEBREPL_PASSWORD` or
  `webreplPassword` in `config.json` is the fallback for a board with none.
- VS Code keeps its own in its secret storage and reads that file too. When
  you type a password there, it asks once whether other tools may have it
  (`mcuctl.sharePasswords`: `ask`, `always` or `never` answers for every
  board). Say yes and the CLI and agents can reach the board you just set up.

WebREPL has no TLS. The password and everything after it cross your network
in the clear, so use it on a network you trust. mcuctl keeps passwords out of
its logs and refuses them in the address (`ws://:pw@host`).

The first connect to a board asks for its password and remembers it for that
board.

## On a weak link

WebREPL serves one session at a time and has no timeout, so a session that was
lost on a bad link can keep the board's only slot long after you stopped. The
next login then connects, gets a `101` and is hung up on before the password
prompt, and mcuctl says the board is holding a session.

mcuctl ends an abandoned login with a close frame and a clean TCP close, so the
board sees it end, and it waits longer for each login step when the link is
slow (at least 5 s, up to 60 s). Set `MCUCTL_WEBREPL_LOGIN_TIMEOUT` to a number
of seconds to fix that wait yourself. If the slot is held anyway, reset the
board, or run `import webrepl; webrepl.stop(); webrepl.start()` on it over USB
serial.

## Limits

- **A loop that never yields.** On stock esp32 firmware, a program that never
  sleeps can't be interrupted over WebREPL and blocks new Wi-Fi connections.
  mcuctl says "The board is busy in a loop that never yields; use serial, or
  reset it." Serial Ctrl-C or a reset still works.
- **One client at a time.** A second connection is hung up on before the
  password prompt. mcuctl retries twice in about a second and then says another
  client may be connected.
- **No `mount`.** Mounting over Wi-Fi is refused: every file the board opens
  goes through the REPL a byte at a time.
- **Connect doesn't soft-reset.** Over Wi-Fi it interrupts only, so the
  program's state survives; a reset would end the connection being made.
- **Power save.** For the length of a transfer mcuctl turns Wi-Fi power saving
  off and restores the board's own value after, unless Bluetooth is active.

How the transport is built and what it costs: [Wi-Fi and Bluetooth transports](../how-it-works/wifi-and-bluetooth.md).
