# Finding the tools you need

mpftp looks for a few things on your machine and never guesses at personal
folder layouts. Each has a fixed search order, and the first match wins. The
order is the same in the editor, the browser interface and the terminal.

## The MicroPython tree

Used by `--mp`, by `mpy-cross` discovery and by builds that need a tree.

1. `mpftp.micropythonPath` (a hint)
2. `MP_DIR`
3. `~/micropython` (`%USERPROFILE%\micropython` on Windows)
4. The firmware workspace and the folders the editor has open: a `micropython/`
   folder in them, or the folder itself if it is the tree
5. In the Firmware panel, **Choose workspace…** (open folders plus Browse)

## The build system

Builds run micropython-pydevices' `build_mp.py`. mpftp looks for the checkout:

1. `--build-system PATH`, or the `mpftp.buildSystemPath` setting, which the
   extension passes as the same hint. A hint that isn't a checkout is an error
   (`discover` reports it as `buildSystemError`), not a fallback, so a typo can't
   quietly build with another checkout.
2. The `buildSystemPath` setting in `~/.mpftp/config.json`, or `MPFTP_BUILD_SYSTEM`.
3. The workspace roots, as the checkout itself or holding `micropython-pydevices/`.
   The CLI passes the current folder and its parents. The extension passes
   `mpftp.workspacePath`, then the open folders.
4. Beside the MicroPython tree: its parent, or a sibling `micropython-pydevices/`.
5. `~/micropython-pydevices`.

What `firmware list` and `firmware modules` show is what that `build_mp.py`
offers. The engine loads it without running `main()` and calls the functions its
prompts use (`ports()`, `boards()`, `variants()`, `module_choices()`, and the
`cp_` ones for CircuitPython) in a child process. A build is one `build_mp.py`
run with no terminal on stdin, so a missing choice stops it with an error rather
than a prompt.

## Cross-compilers and SDKs

`build_mp.py` fetches ESP-IDF and emsdk at its locked versions. The engine still
checks for a cross-compiler a port needs on `PATH` (`arm-none-eabi-gcc`, MinGW
and so on) and answers `needToolchain` with instructions to Locate… or install
when one is missing. `mpftp.idfPath`, `mpftp.emsdkPath` and
`mpftp.toolchainBins` override the defaults. mpftp does not add well-known home
folders that special-case one vendor's SDK.
