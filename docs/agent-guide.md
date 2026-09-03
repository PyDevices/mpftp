# Agent Guide — using mpftp with MicroPython / CircuitPython boards

This document is for **coding agents** (and humans driving the same CLI) that need
to talk to a board, push Python, rebuild MicroPython firmware with user C modules,
or flash for recovery. Prefer the **extension TCP RPC** so you share the UI’s serial
session and do not open a second connection on the same port.

**Serial** works for both MicroPython and CircuitPython. **Firmware**
download/build/flash stays MicroPython-only.

> **Note:** every example below uses `./scripts/mpftp` (clone-only). If
> `pydevices-mpftp` is pip-installed instead, `mpftp` is on `PATH` — use
> plain `mpftp` in place of `./scripts/mpftp` everywhere in this guide.

## Session model

| Path | Purpose |
|------|---------|
| `~/.mpftp/workspace-rpc.json` | **Preferred** — map of workspace root → RPC (`cwd` match); no files in the repo |
| `MPFTP_RPC` env | Override (`127.0.0.1:7430`) if multiple windows compete |
| `~/.mpftp/sessions/<id>.pid` | Per-window sidecar claim (`MPFTP_SESSION_ID`); windows do not kill each other |
| `~/.mpftp/activity.log` | NDJSON of connects, transfers, RPC, errors |
| `~/.mpftp/repl.log` | REPL I/O when a REPL is open |

The Cursor/VS Code window must have **mpftp loaded** for the socket to exist.
mpftp does **not** create `<workspace>/.mpftp/` in open folders.

**Concurrent two boards (two windows, two workspace folders):** each window
runs its own sidecar (`MPFTP_SESSION_ID` / `~/.mpftp/sessions/<id>.pid`) and
Agent RPC. Agents should use a cwd inside the matching workspace so CLI hits
that root in `~/.mpftp/workspace-rpc.json`. Connect each window to a
**different** COM port. The same COM remains exclusive (second connect gets
port busy). Reloading one window must not kill the other's sidecar.

**Two editors (or two windows) on the *same* workspace folder** is a
different situation — `workspace-rpc.json` has one entry per path, so
whichever window connects most recently wins that key; the other's session
becomes unreachable via cwd-based discovery with no error. This was tracked
in [PyDevices/mpftp#21](https://github.com/PyDevices/mpftp/issues/21),
fixed — the collision itself is now logged (see below), though the
one-entry-per-path registry design is unchanged, so it's still worth
knowing about. Run
`mpftp status`: the `editor`/`pid`/`updatedAt` fields on the resolved entry
say whose session it actually is (`extension_running: true` alone does not).
A logged `rpc_registry_collision` in `~/.mpftp/activity.log` marks the
moment one editor's registration overwrote a different, still-recent one.

On WSL, serial and esp32 flash use **Windows Python** so `COM` ports work.
Install host packages on that interpreter: `mpremote`, and **`circup`** for
CircuitPython library installs (`python.exe -m pip install mpremote circup`).

```bash
chmod +x scripts/mpftp
./scripts/mpftp status          # rpc up? connected device? interpreter?
./scripts/mpftp watch           # follow activity.log
```

Standalone (no extension): pass `-d/--device` after the subcommand. Prefer RPC
while the UI is connected.

---

## Board workflow (day-to-day)

### Connect

```bash
./scripts/mpftp ports
./scripts/mpftp connect COM4        # or /dev/ttyACM0
./scripts/mpftp resume              # last device
```

Connect **interrupts** any running program and enters raw REPL. Interpreter is
detected from `sys.implementation.name` and returned as `interpreter`
(`micropython` | `circuitpython`).

| Interpreter | `soft-reset` | `soft-reboot` |
|---------|---------------|--------------|
| MicroPython | Raw soft-reset — skips `main.py` | Friendly Ctrl-D — runs `main.py` |
| CircuitPython | Friendly↔raw toggle — **does not** Ctrl-D | Ctrl-D — runs `code.py` |

CircuitPython may show “Press any key to enter the REPL…”; mpftp sends a key
before raw. Prefer CDC REPL ports (CDC2 data interfaces are filtered).

If connect fails with a filesystem-corruption banner (MicroPython), the board may
need erase + reflash (see Troubleshooting).

### Board filesystem & REPL

Treat the board like a small filesystem. Prefer verified transfers for anything
that must land intact. Startup script is usually `main.py` (MP) or `code.py` (CP).

```bash
./scripts/mpftp ls /
./scripts/mpftp tree /
./scripts/mpftp put ./main.py /main.py --verify
./scripts/mpftp get /main.py ./main.py --verify
./scripts/mpftp cp ./lib :/lib --verify     # : = board path
./scripts/mpftp put ./app.py /app.py --mpy  # compile to .mpy via mpy-cross (MicroPython only)
./scripts/mpftp cp ./lib :/lib --mpy        # same, recursively; boot.py/main.py stay source
./scripts/mpftp mkdir /lib
./scripts/mpftp rm /junk.py
./scripts/mpftp eval '1+1'
./scripts/mpftp exec 'print(1)'             # waits for EOF; use --no-follow for loops
./scripts/mpftp run ./app.py                # default --no-follow (UI-safe)
./scripts/mpftp run ./short.py --follow     # wait for script to finish
./scripts/mpftp watch-repl                  # live tail of stdout; never interrupts
./scripts/mpftp interrupt                   # Ctrl-C; no reset
./scripts/mpftp soft-reset                  # MP: skip main.py (see table)
./scripts/mpftp soft-reboot                 # Ctrl-D; runs main.py / code.py
./scripts/mpftp hard-reset
./scripts/mpftp debug-tee COM50             # second port read-only (native USB CDC)
./scripts/mpftp mip github:org/repo         # MicroPython only (default target /lib)
./scripts/mpftp circup adafruit_display_text  # CircuitPython only → /lib over serial
```

**Packages**

| | MicroPython | CircuitPython |
|---|---|---|
| CLI | `mpftp mip …` | `mpftp circup …` |
| Host dep | `mpremote` | `circup` on the sidecar Python |
| Transport | serial (host download → board write) | **Web Workflow preferred** (`circup --host` when Wi‑Fi + `CIRCUITPY_WEB_API_PASSWORD` are set); else host stage → serial put / CIRCUITPY MSC |

`mount` / `umount` / `romfs` remain **MicroPython-only**.

**Compile-on-upload (`.mpy`)**

`--mpy` (alias `--compile`) on `put` / `cp`, and the File Transfer UI's
`mpftp.compileOnUpload` setting, compile `.py` files to `.mpy` with
`mpy-cross` before writing them to the board — smaller flash footprint,
faster imports, no on-device parse/compile. **MicroPython only** —
CircuitPython uses different bytecode/tooling; requesting `--mpy` against a
CircuitPython board (via the CIRCUITPY USB drive) is a clear error, not a
silent `.py` fallback.

- `boot.py` / `main.py` are never compiled (`mpftp.mpyExcludeFiles` in
  `~/.mpftp/config.json`, default `["boot.py", "main.py"]`, to add more).
- `mpy-cross` discovery order: the firmware workspace's own build
  (`<micropython>/mpy-cross/build/mpy-cross`, resolved the same way as
  `mpftp.workspacePath` / `mpftp.micropythonPath` for firmware builds) → `PATH`
  (covers `pip install mpy-cross`) → a clear error naming both fixes.
- Before compiling, the connected board's `sys.implementation._mpy` byte is
  compared against what the resolved `mpy-cross --version` reports it emits.
  A mismatch fails clearly rather than uploading bytecode the board can't
  load — use the `mpy-cross` built from the same MicroPython tree as the
  board's firmware. Boards that don't expose `_mpy` skip the check.
- A directory `cp --mpy` uploads a mix of `.mpy` (compiled) and `.py`
  (excluded/non-Python) files as appropriate; the result's `copied` list
  reflects the actual remote names written.

**CircuitPython file transfers**

While the **CIRCUITPY** USB drive is mounted on the host, `put` / `cp` / mkdir /
rm write through that volume (USB MSC) — the same default workflow as Mu/Thonny
and circup ``--path``. Serial writes stay available when MSC is not mounted
(or after `storage.disable_usb_drive()` in `boot.py`).

**CircuitPython packages (no ``boot.py`` required)**

With Wi‑Fi in `/settings.toml` (`CIRCUITPY_WIFI_*`, `CIRCUITPY_WEB_API_PASSWORD`),
`mpftp circup` picks the fastest available transport:

1. **CIRCUITPY mounted** → `circup --path` (USB disk; no board edits)
2. **Web Workflow writable** → `circup --host` (Wi‑Fi; needs MSC *not* locking the FS)
3. Else host stage + serial / MSC copy

While USB mass storage is enabled in firmware, CircuitPython keeps the FS
read-only for the device — host “Eject” is not enough for Wi‑Fi writes. Prefer
a mounted CIRCUITPY drive, or optionally `storage.disable_usb_drive()` in
`boot.py` if you want Web Workflow with the cable plugged in.

**Watching a long-running script without killing it**

`get`/`put`/`exec` all enter raw REPL, which Ctrl-Cs whatever's running — there
is no way around that for reading an arbitrary board file. To watch progress
instead:

1. `run --no-follow script.py` — starts the script and returns immediately.
2. `watch-repl` — subscribes to the board's stdout live, never touches raw
   REPL. Have the script `print()` its own progress. Ctrl-C on the host stops
   *watching*; the board keeps running.

If you need an actual file's final contents (not just progress), have the
script write it, then `get` it once the script is done — that `get` still
interrupts, but only once, at the point you actually wanted the result.
`probe` bundles exactly this cycle into one command:

```bash
./scripts/mpftp probe -d COM4 probe.py --reboot-first --capture /result.txt --wait 20
```

`--reboot-first` hard-resets and reconnects before running — stale module
state from a previous iteration (re-imported `board_config`, armed timers)
otherwise makes an otherwise-fine board look broken a couple of iterations
in. Requires `--device` since it's what `probe` reconnects to after the
reset. `--wait` is a flat delay before the capture read; `--capture` is
optional (omit it to just run and move on). The result is JSON either way —
a capture failure sets `"ok": false` and a `capture_error` key rather than
raising past the point where you'd lose the fact that the run itself
succeeded.

**Rules of thumb**

- Debug with `exec` / `eval` / `run` before rewriting `main.py` / `code.py`.
- Soft-reset after bad imports; hard-reset if the port is wedged.
- Dotfiles / `__pycache__` are skipped by the File Transfer UI; CLI `put` of a
  single path does what you ask.
- Do not put secrets in scripts that will show up in `activity.log`.

---

## CircuitPython specifics

CircuitPython behaves differently enough from MicroPython that assuming parity
will cost you hours. The differences below were all found the hard way.

### File operations do not interrupt the running program

On a CircuitPython board, `put` / `get` / `ls` / `hash` are routed through the
mounted `CIRCUITPY` volume instead of the serial port. The response says so:

```bash
./scripts/mpftp put -d COM59 ./probe.py /probe.py
# {"path": "/probe.py", "size": 18, "via": "circuitpy_msc"}
```

This is the single most useful fact for an agent working on CircuitPython. On
MicroPython, reading a board **file** to see how a long-running script is
doing is what kills it — every `exec` / `get` / `put` enters the raw REPL and
Ctrl-Cs whatever is running. There is no protocol-level way around that for an
arbitrary file (raw REPL is the only way to run code that opens one). What you
*can* do without ever touching raw REPL is watch what the script itself
prints: `mpftp watch-repl` subscribes to the board's stdout and streams it live
— Ctrl-C there stops watching, not the board. Have the script `print()`
progress instead of (or in addition to) writing a result file, and there's no
"stall at stage 00 because reading it just killed it" trap to fall into. If you
do need a file's contents specifically, the write-to-file pattern under *Board
filesystem & REPL* is still the way, with the accepted cost that the `get` at
the end interrupts once. On CircuitPython you can just read the file — no
raw-REPL trip needed either way.

Two caveats:

- The volume must actually be mounted. Under WSL, `/mnt/d` will **not** exist —
  Windows does not auto-mount removable drives into WSL — so the sidecar uses the
  Windows path (`D:\`) with Windows Python. That is handled for you.
- The initial `connect` still interrupts once, because the interpreter has to be
  detected before the routing decision can be made.

`boot_out.txt` on the volume identifies the board without any serial contact, and
its `UID` equals the port's `serial_number`, so a volume can be matched to a COM
port offline:

```
Adafruit CircuitPython 10.2.1 on 2026-08-21; Waveshare RP2040-TOUCH-LCD-1.28
Board ID:waveshare_rp2040_touch_lcd_1_28
UID:E462A052C73E4A29
```

### The board cannot write its own filesystem

While USB MSC is exposed, `CIRCUITPY` is writable by the **host** and read-only
to the **board**. The MicroPython trick of having a script append progress to
`/result.txt` does not work here. Use instead:

```python
import supervisor
supervisor.get_previous_traceback()   # traceback from the previous code.py run
```

read from the REPL afterwards — it survives the run that produced it.
`supervisor.set_next_code_file()` chooses what runs next.

### Do not assume auto-reload

`supervisor.runtime.autoreload` is often **False** (CircuitPython prints
`Auto-reload is off.` at the top of each run). Writing to the volume then does
*not* restart the board. Check it rather than relying on a file write to trigger
a run.

### Entering the bootloader

`mpftp bootloader` picks the right mechanism per interpreter — `machine.bootloader()`
on MicroPython, `microcontroller.on_next_reset(RunMode.BOOTLOADER)` plus
`microcontroller.reset()` on CircuitPython — and reports `ok: false` when it could
not reach the REPL rather than claiming success.

When the board is wedged so badly that Ctrl-C cannot reach the interpreter (see
below), the REPL path cannot work by definition. On UF2 boards, opening the port
at **1200 baud with DTR low** enters the bootloader from the USB stack, which
keeps running when the VM does not:

```python
import serial, time
s = serial.Serial("COM59", 1200, timeout=0.3); s.dtr = False
time.sleep(0.3); s.close()          # board re-enumerates as RPI-RP2
```

Copy a `.uf2` onto that volume to get back. On RP2040 the `CIRCUITPY`
filesystem **survives** the reflash — firmware and filesystem are separate
regions — the same reassurance as flashing ESP32 at `0x2000`.

### Ctrl-C is not an interrupt inside `atexit`

CircuitPython does not arm Ctrl-C as an interrupt character while an `atexit`
handler runs; it arrives as ordinary stdin data. A handler that loops without
reading stdin therefore fills the USB CDC receive ring, at which point **host
writes start timing out** and no tool can reach the board. `connect` hangs, and
the 1200-baud touch above is the only way back.

If you write board-side code that holds the VM this way, drain the ring each
pass:

```python
import sys, supervisor
rt = supervisor.runtime
waiting = rt.serial_bytes_available
if waiting and "\x03" in sys.stdin.read(waiting):
    ...   # treat as quit
```

### Firmware building is out of scope

`mpftp firmware *` is MicroPython-only — there is no CircuitPython support in
`firmware_engine.py` at all. Build CircuitPython with its own toolchain.

---

## Firmware: diagnose, download, build, flash

Firmware commands are **host-side** and **MicroPython-only**. They do not
hold the serial lock for the whole build. CircuitPython firmware is out of scope
for mpftp — see [CircuitPython specifics](#circuitpython-specifics), which also
covers entering the bootloader on UF2 boards.
### Detect (troubleshooting first step)

Works on a bare board (no MicroPython). Releases a live session briefly if needed.

```bash
./scripts/mpftp firmware detect -d COM4
```

Use chip / flash size / secure-boot / flash-encryption state before flashing.
If secure boot or flash encryption is enabled, stop and confirm before erase.

### Official download (no local checkout)

```bash
./scripts/mpftp firmware download-tree
# UI: Firmware → Download → pick board/version → Download → Flash
```

### Build from a firmware workspace

A **firmware workspace** must provide MicroPython: `micropython/` (dir or
symlink) or the folder *is* the tree (`ports/` + `py/`). Port SDKs
(`esp-idf`, `emsdk`, …) must be **in that workspace (or symlinked)** or set via
env vars — same contract for every dependency, no special home-path hunts.

User modules and aggregators: see **[aggregator.md](aggregator.md)**.

```bash
./scripts/mpftp firmware discover
./scripts/mpftp firmware list
./scripts/mpftp firmware cmods
./scripts/mpftp firmware build --port esp32 --board ESP32_GENERIC_P4 --variant C6_WIFI
./scripts/mpftp firmware artifact --port esp32 --board ESP32_GENERIC_P4 --variant C6_WIFI
./scripts/mpftp firmware flash --port esp32 --board ESP32_GENERIC_P4 --variant C6_WIFI -d COM4
# erase when recovering a corrupt filesystem / wrong partition layout:
# (Firmware UI → Erase, or engine --erase)
```

Flash without rebuild to the next board:

```bash
./scripts/mpftp firmware flash -d COM5
```

Supported flashers: `esp32` (esptool), `rp2` / `samd` (UF2; BOOTSEL first).

### Flashing over UF2

`rp2` and `samd` take the UF2 path automatically. `--uf2` forces it for any
port, which is what reaches a board whose UF2 bootloader is not implied by its
MicroPython port — an ESP32-S3 carrying tinyuf2, most usefully:

```bash
./scripts/mpftp bootloader -d COM7          # or double-tap reset / hold BOOTSEL
./scripts/mpftp firmware flash --port esp32 --uf2 --artifact build/firmware.uf2
```

The artifact must be a `.uf2`; the command refuses a `.bin` rather than copying
something the bootloader will ignore.

**The copy is not the proof.** A UF2 flash has two failure modes that both look
exactly like success from the host: a copy that reports fine while writing
nothing, and a bootloader that silently skips every block whose family ID it
does not own. So the engine validates the file first (magic, block count against
the header, family IDs), verifies the byte count it wrote, and then waits for
the bootloader volume to **unmount** — which only happens once the board has
accepted a complete image and rebooted into it. A volume still mounted after
`--uf2-timeout` seconds (default 30) is reported as a failure naming the image's
family, because a family mismatch is the usual cause.

Volumes are found by `INFO_UF2.TXT`, not by label, since labels differ per family
(`RPI-RP2`, `FTHRS3BOOT`, …). If more than one is mounted the command stops and
asks for `--device` rather than guessing which board to overwrite. On WSL a
removable drive is usually *not* mounted under `/mnt`, so discovery also asks
Windows for drive letters; `--device 'D:'` works there too.

### ESP32 partition autosize

If an esp32 build fails because the app image is larger than the `factory` (or
other app) partition, mpftp **parses the overflow**, writes a grown table to
`<workspace>/esp32_partitions/<board>.csv` (sibling of `micropython/` — the
MicroPython tree is never edited), patches the build-dir `sdkconfig`, and
**rebuilds once**. Disable with `--no-autosize`.

Details: [user guide — Autosize](user-guide.md#esp32-partition-autosize).

---

## Error handling

A command that fails prints `{"ok": false, "error": "...", "hint": "..."}` to
stdout (the `hint` key is present only when there's an actionable next step
that isn't already in `error`) and exits non-zero — the same shape a
successful command's JSON result has, so parse stdout as JSON either way
instead of branching on exit code first. When `run --follow` (the
`run_script` / `run_path` RPC methods with `follow=true`) times out waiting
for the script to finish, the envelope also carries `"partialOutput"`:
everything the board printed before it stopped, so the last line points at
where it hung.

## Troubleshooting playbook

| Symptom | Agent action |
|---------|----------------|
| Port busy / exclusive lock | Another mpftp window may own that COM — disconnect there, or pick the other board. Also close Thonny/serial monitors. Do not expect two windows on one COM |
| Two windows / two boards | Use distinct COM ports; run CLI from each workspace cwd (`workspace-rpc.json`). Check `mpftp status` → distinct `rpc` + `session_id` |
| Two editors on the *same* workspace folder — CLI reaches an unexpected session | One-entry-per-path registry; most recent connect wins — was tracked in [PyDevices/mpftp#21](https://github.com/PyDevices/mpftp/issues/21), fixed: no longer silent. `mpftp status` → check `editor`/`pid`/`updatedAt` on the resolved entry |
| `Access is denied` / `transport_dead` after hung `exec`/`run` | Sidecar releases the COM handle automatically (was tracked in [PyDevices/mpftp#3](https://github.com/PyDevices/mpftp/issues/3), fixed via a bounded serial write-timeout); `disconnect` then `resume`/`connect`. If still busy: reload extension window, then replug USB only as last resort |
| `timeout waiting for first EOF` | Board still running (UI loop). Use `run` without `--follow` / `exec --no-follow`, then `interrupt` or `soft-reset` |
| Soft-reset left UI dead after deploy | Expected: soft-reset skips `main.py`. Use `soft-reboot` or `hard-reset` to run startup |
| Dual USB (UART + native CDC) | `mpftp ports` shows `role` (`repl` vs `cdc_debug`); control on UART, `debug-tee` on CDC |
| `could not enter raw repl` after flash | Detect; erase + reflash MicroPython; corrupt FS boot loops block soft-reset |
| Wrong board / no Wi-Fi on P4 | Detect + MicroPython hints; pick `C5_WIFI` / `C6_WIFI` explicitly if needed |
| Build: required tree not found | Symlink under firmware workspace or set env (`IDF_PATH`, `EMSDK`, …); Locate… in UI |
| App partition too small | Let autosize rebuild once, or adjust `esp32_partitions/<board>.csv` |
| Module missing from firmware | See [aggregator.md](aggregator.md); `firmware cmods` |
| CircuitPython: every command hangs, serial **writes** time out | Board is wedged with the CDC receive ring full — Ctrl-C cannot reach it. 1200-baud touch with DTR low → UF2 volume → copy firmware. See [CircuitPython specifics](#circuitpython-specifics) |
| CircuitPython: reading a file killed my running script | It should not — file ops route over the `CIRCUITPY` volume (`"via": "circuitpy_msc"`). If you see raw-REPL behaviour instead, the volume is not mounted |
| CircuitPython: wrote to the volume, board did not restart | `supervisor.runtime.autoreload` is likely False; reset explicitly instead |
| CircuitPython: display blanks the moment the script ends | Expected — the supervisor runs `reset_port()` when the VM finishes, releasing pins and resetting the panel. The app has to hold the VM |

---

## RPC reminder

```bash
./scripts/mpftp rpc ping
./scripts/mpftp rpc fs_listdir '{"path":"/"}'
# Firmware methods are also exposed over the same agent RPC when the extension runs.
```

## MCP server

`python -m mpftp.mcp` (or the `mpftp-mcp` console script) is a stdio MCP
server exposing this same session as 25 typed tools — `list_ports`/
`connect`/`disconnect`, `fs_*`, `exec_code`/`eval_expr`/`run_script`/
`run_path`, `watch_repl`/`probe`, `interrupt`/`soft_reset`/`soft_reboot`/
`hard_reset`, and `firmware_*` — for agent hosts that talk MCP instead of
this CLI. Same `RpcClient` underneath (extension RPC when one's running, a
private sidecar otherwise), so it shares session semantics with the CLI
exactly. See [`integrations/`](../integrations/) for a Claude Code CLI
plugin (bundles the server plus a condensed version of this guide as a
skill), a Claude Desktop app extension (MCPB — a different install path;
note its "Local session required" caveat), and a Codex CLI config snippet.

See [user-guide.md](user-guide.md), [aggregator.md](aggregator.md), and
[developers-guide.md](developers-guide.md). Keep this file aligned when
CLI or discovery contracts change.
