# Transfer files

You move files with a two-pane File Transfer view in the editor or the
[browser interface](use-the-browser-interface.md), or with commands.

## In the File Transfer view

Open **File Transfer** as an editor tab or in the panel. Drag files between
the Local and Board panes, or use the buttons over each list. Double-click a
board file to edit it: saving writes it back, with an optional SHA-256 check
(`mpftp.verifyTransfers`).

To keep a copy somewhere else, run **mpftp: Save As…** (the title bar's
save-as button, or right-click in the editor). Pick **This computer** or
**Board**, then edit the path, which starts in that list's current folder. The
editor then shows the copy, so the next save goes there and the original stays
as it was. It asks before replacing a file, and the folder has to exist.

## From the terminal

```bash
mpftp ls /
mpftp tree /
mpftp put ./main.py /main.py            # verified by SHA-256 by default
mpftp get /main.py ./main.py
mpftp cp ./lib :/lib                    # a leading : marks the board side
mpftp mkdir /lib
mpftp rm /junk.py
mpftp hash /main.py
mpftp df
```

`put`, `get` and `cp` verify with SHA-256 unless you pass `--no-verify`. Add
`-r` to `put` and `get` to copy a folder.

A folder copy works like `cp -r`. When the destination folder doesn't exist
yet, it's created and gets the folder's contents. When it does exist, the
folder goes inside it. So to update a library already on the board, put it
into the folder above:

```bash
mpftp put -r ./cast /cast     # first time: creates /cast
mpftp put -r ./cast /         # again: replaces the files in /cast
```

Putting `./cast` to `/cast` a second time writes `/cast/cast/...` and leaves
the old files where your code imports them from. The folder above the
destination has to exist: make it first with `mpftp mkdir`.

## Compile on upload

`--mpy` on `put` or `cp` (and the `mpftp.compileOnUpload` setting for the
view) compiles `.py` files to `.mpy` with `mpy-cross` before writing them, for
a smaller footprint and faster imports. It is for MicroPython only. Against a
CircuitPython board over its USB drive it is an error, not a silent `.py`
upload.

- `boot.py` and `main.py` are never compiled. `mpyExcludeFiles` in
  `~/.mpftp/config.json` changes that list.
- mpftp looks for `mpy-cross` and then `mpy-cross.exe`, first in the firmware
  workspace's MicroPython build (`mpy-cross/build/`), then on `PATH`
  (`pip install mpy-cross` puts one there). If none runs, the error names both
  fixes.
- Before compiling, it compares the board's `sys.implementation._mpy` with
  what `mpy-cross --version` emits, and refuses a mismatch rather than upload
  bytecode the board can't load. Use the `mpy-cross` built from the same
  MicroPython tree as your firmware. A board without `_mpy` skips the check.
- A folder copy writes a mix of `.mpy` and untouched `.py` files. The result's
  `copied` list names what was written.

## CircuitPython

While the **CIRCUITPY** drive is mounted on the host, file commands write
through that drive (USB mass storage), the way Mu and circup do. Serial writes
take over when the drive isn't mounted or the board hides it with
`storage.disable_usb_drive()`. [Work with CircuitPython](work-with-circuitpython.md)
says why that matters for a running program.
