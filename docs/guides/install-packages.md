# Install packages

mpftp installs with `mip` on MicroPython and `circup` on CircuitPython, chosen
by the interpreter it detected.

```bash
mpftp mip github:org/repo                  # MicroPython; installs to /lib by default
mpftp mip NAME --index https://PyDevices.github.io/mip
mpftp omip NAME                            # MicroPython; an OmniPython package and what it needs
mpftp circup adafruit_display_text         # CircuitPython; installs to /lib
```

In the editor and the browser interface, **mpftp: Install Package** runs `mip`
or `circup`. `omip` is on the command line.

## Install an OmniPython package

`mpftp omip NAME` looks on the OmniPython index and then on micropython-lib, and
brings what the package names from micropython-lib as well. `mip` can't do that:
it takes every file from the one index it was given, so `mpftp mip` keeps doing
what `mip` does. The computer does the fetching, so the board needs no network.

```bash
mpftp omip palettes
mpftp omip fnmatch --no-mpy --target /flash/lib
```

Everything is downloaded before the first file is written, so a package that
can't be found leaves the board as it was. `--index` looks for the package on
that index alone; what it needs from micropython-lib still comes from there.

To let the board install for itself, put `omip.py` on it:

```bash
mpftp omip --self
```

Then, on the board, with Wi-Fi:

```python
import omip
omip.install("palettes")
```

`mpftp omip --self` works on CircuitPython too, through the drive when it is
mounted. Naming a package there is refused: on CircuitPython, install from the
computer with `mpftp circup`.

## Find a package

If you don't know the exact name, search by words from its name or
description:

```bash
mpftp search display text --text
```

The first hit looks like this:

```text
adafruit_display_text 5.0.6  [Adafruit CircuitPython Bundle] — Displays text using CircuitPython's displayio.
    mpftp circup adafruit_display_text
```

Search needs no board. It covers micropython-lib, the PyDevices index and the
CircuitPython bundles (Adafruit and community); with a board connected it keeps
to the indexes that board installs from. In the editor it is **mpftp: Search
Packages…**, and also in the ⋯ menu of the File Transfer view and the browser
interface. A PyDevices package's install line carries
`--index https://PyDevices.github.io/mip`, which `mip` needs to find it.

Without `--text` the answer is JSON: each result has `name`, `source`,
`version`, `description` and an `install` with the `command` to run. Pass
`--interpreter` to keep to what one interpreter can install, `--source` to search one index and `--limit` to change the 25 results it shows. The indexes are
cached for six hours under `~/.mpftp/package-cache`; `--refresh` fetches them
again, and with no network the search runs on the cached copies and says so.

## How CircuitPython installs

`mpftp circup` picks the fastest route available:

1. With **CIRCUITPY** mounted, `circup --path` writes to the drive.
2. Otherwise, if Wi-Fi is up and `/settings.toml` holds `CIRCUITPY_WIFI_*` and
   `CIRCUITPY_WEB_API_PASSWORD`, and the filesystem is writable over the web
   workflow, `circup --host` installs over Wi-Fi. While USB storage is enabled,
   CircuitPython keeps the filesystem read-only to the board, so the host
   "Eject" button isn't enough; use the mounted drive, or call
   `storage.disable_usb_drive()` in `boot.py` if you want the web workflow with
   the cable plugged in.
3. Otherwise, mpftp stages the package on the host and copies it over serial.

`circup` has to be installed on the same Python as mpftp's sidecar.
