# Connect over Wi-Fi

Once a board is set up, you can reach it without a cable. Every board command
takes `-d ws://HOST[:8266]`, and the REPL, file transfer, run, exec and resets
all work as they do over serial. The editor's Connect list and the browser
interface list the board under **Wi-Fi** by name.

## Set a board up

Connect over USB once and run **mpftp: Enable Wi-Fi Access** (the browser
interface's **Wi-Fi access…** button, or `mpftp wifi enable -d PORT`). You
choose a WebREPL password. mpftp shows you the exact change to `boot.py` and
writes nothing until you say yes. After that the board joins your network at
every reset.

The change is a block between `# >>> mpftp wifi-access >>>` and
`# <<< mpftp wifi-access <<<` at the top of `boot.py`. It imports the board's
`wifi` helper, calls `wifi.connect_from_secrets()` so the board reads its own
`secrets.py`, then starts `webrepl` with your password. mpftp never reads
`secrets.py`; it only checks one exists. Anything missing (no helper, no
`secrets.py`, no `webrepl`) is explained instead of written, and mpftp refuses
to write if `boot.py` changed after you looked. **Disable Wi-Fi access**
(`mpftp wifi disable`) removes the block and gives back `boot.py` byte for
byte; if mpftp created the file, it deletes it.

## Connect

```bash
mpftp wifi boards                          # boards mpftp has seen with Wi-Fi up
mpftp wifi find mpy-board                # NAME.local by mDNS, best effort
mpftp exec -d ws://BOARD-IP "print('hello')"
mpftp put -r -d ws://BOARD-IP lib /lib
mpftp get -r -d ws://BOARD-IP /lib ./lib-copy
```

A serial connect to a board with Wi-Fi up records it under `wifiBoards` in
`~/.mpftp/config.json`, and that is where the Connect lists get their names.
You can also type an address, or a `NAME.local` that mpftp looks up by mDNS.

## Passwords

WebREPL keeps at most 9 characters, and a password mpftp writes to a board is
4 to 9. Each board has its own, kept in one of these places:

- The CLI and the browser interface keep them in
  `~/.mpftp/webrepl-passwords.json`, plaintext, readable only by you.
  `mpftp wifi password BOARD` saves one. `MPFTP_WEBREPL_PASSWORD` or
  `webreplPassword` in `config.json` is the fallback for a board with none.
- VS Code keeps its own in its secret storage and reads that file too. When
  you type a password there, it asks once whether other tools may have it
  (`mpftp.sharePasswords`: `ask`, `always` or `never` answers for every
  board). Say yes and the CLI and agents can reach the board you just set up.

WebREPL has no TLS. The password and everything after it cross your network
in the clear, so use it on a network you trust. mpftp keeps passwords out of
its logs and refuses them in the address (`ws://:pw@host`).

The first connect to a board asks for its password and remembers it for that
board.

## On a weak link

WebREPL serves one session at a time and has no timeout, so a session that was
lost on a bad link can keep the board's only slot long after you stopped. The
next login then connects, gets a `101` and is hung up on before the password
prompt, and mpftp says the board is holding a session.

mpftp ends an abandoned login with a close frame and a clean TCP close, so the
board sees it end, and it waits longer for each login step when the link is
slow (at least 5 s, up to 60 s). Set `MPFTP_WEBREPL_LOGIN_TIMEOUT` to a number
of seconds to fix that wait yourself. If the slot is held anyway, reset the
board, or run `import webrepl; webrepl.stop(); webrepl.start()` on it over USB
serial.

## Limits

- **A loop that never yields.** On stock esp32 firmware, a program that never
  sleeps can't be interrupted over WebREPL and blocks new Wi-Fi connections.
  mpftp says "The board is busy in a loop that never yields; use serial, or
  reset it." Serial Ctrl-C or a reset still works.
- **One client at a time.** A second connection is hung up on before the
  password prompt. mpftp retries twice in about a second and then says another
  client may be connected.
- **No `mount`.** Mounting over Wi-Fi is refused: every file the board opens
  goes through the REPL a byte at a time.
- **Connect doesn't soft-reset.** Over Wi-Fi it interrupts only, so the
  program's state survives; a reset would end the connection being made.
- **Power save.** For the length of a transfer mpftp turns Wi-Fi power saving
  off and restores the board's own value after, unless Bluetooth is active.

How the transport is built and what it costs: [Wi-Fi and Bluetooth transports](../how-it-works/wifi-and-bluetooth.md).
