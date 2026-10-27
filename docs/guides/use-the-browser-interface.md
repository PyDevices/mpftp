# Use the browser interface

`python -m mpftp` opens mpftp in your browser: the same File Transfer panel as
the VS Code extension, the REPL under it, and an editor beside them. You don't
need VS Code, and you can install it as an app with its own window and icon.

Start it in the folder you want on the Local side:

```bash
cd ~/projects/blink
python -m mpftp
```

It serves itself at `http://127.0.0.1:8317/` and opens a browser tab. Stop it
with Ctrl+C. To pick another port or skip the tab, run
`python -m mpftp.pwa --port 9000 --no-open` (`MPFTP_PWA_PORT` sets the port
too). `python -m mpftp` followed by any subcommand is still the regular CLI.

The Local list is the computer running `python -m mpftp`, and that computer
is the only place the page can be opened from, because it listens on
loopback. It is a board session of its own, separate from the VS Code
extension's, so connect a board in one or the other, not both.

## What you can do

- **Connect** with the plug button. Pick a USB serial port, a Wi-Fi board
  mpftp remembers, an address you type, or a Bluetooth board. The status
  line shows the port and the badge shows MicroPython or CircuitPython.
- **Move files** between the two lists. Select on either side (Ctrl+click and
  Shift+click pick several, folders included) and press an arrow, or drag from
  one list to the other. Folders go with everything inside them, except
  `.git`, `__pycache__`, `*.pyc` and other dot-entries. Each file is checked
  with SHA-256 afterwards (`verifyTransfers` in `~/.mpftp/config.json` turns
  that off).
- **Manage files** with the buttons over each list or a right-click: new
  folder, new file, rename, delete. Delete asks first.
- **Edit** a file: double-click a board file, or select a file on either side
  and press Open in Editor. Each file gets a tab (board files have a blue
  dot), and Ctrl+S writes it back to where it came from. Save As
  (Ctrl+Shift+S) saves a copy on this computer or the board, and the tab
  follows the copy. Double-clicking a local file uploads it, as in VS Code.
- **Run** code: the play button runs a board `.py`, or uploads and runs a
  local one, and its output appears in the REPL. The `⋯` menu has the rest:
  interrupt, soft and hard reset, eval and exec, `mip` and `circup` installs,
  package search, the RTC, `df`, Wi-Fi access and running the editor's file.
- **Plot** what the board prints: the Plotter button over the REPL opens a
  graph beside it. Every line that is only numbers adds a sample, one series
  per number: `print(t)`, `print(t, h, p)` (commas or spaces), or named,
  `print(f"temp:{t} hum:{h}")` (also `temp: 21.5` or `temp=21.5`). It plots up
  to eight series and the newest 500 samples, scaled to fit. Hover for the
  values at a sample. Pause stops adding samples, Clear starts again, and
  other REPL output is left alone.
- **Flash firmware** with the Firmware button; see
  [Download and flash firmware](download-and-flash-firmware.md).
- **Install it as an app**: Chrome and Edge offer Install in the address bar.
  The app still needs `python -m mpftp` running to reach your boards.
