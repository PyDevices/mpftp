# Agent Guide — using mpftp with MicroPython / CircuitPython boards

This document is for **coding agents** (and humans driving the same CLI) that need
to talk to a board, push Python, rebuild MicroPython firmware with user C modules,
or flash for recovery. Prefer the **extension TCP RPC** so you share the UI’s serial
session and do not open a second connection on the same port.

**Serial** works for both MicroPython and CircuitPython. **Firmware**
download/build/flash stays MicroPython-only.

## Session model

| Path | Purpose |
|------|---------|
| `~/.mpftp/workspace-rpc.json` | **Preferred** — map of workspace root → RPC (`cwd` match); no files in the repo |
| `~/.mpftp/rpc.port` | Fallback when cwd is not in the workspace registry |
| `MPFTP_RPC` env | Override (`127.0.0.1:7430`) if multiple windows compete |
| `~/.mpftp/sessions/<id>.pid` | Per-window sidecar claim (`MPFTP_SESSION_ID`); windows do not kill each other |
| `~/.mpftp/activity.log` | NDJSON of connects, transfers, RPC, errors |
| `~/.mpftp/repl.log` | REPL I/O when a REPL is open |

The Cursor/VS Code window must have **mpftp loaded** for the socket to exist.
mpftp does **not** create `<workspace>/.mpftp/` in open folders.

**Concurrent two boards (two windows):** each window runs its own sidecar
(`MPFTP_SESSION_ID` / `~/.mpftp/sessions/<id>.pid`) and Agent RPC. Agents should
use a cwd inside the matching workspace so CLI hits that root in
`~/.mpftp/workspace-rpc.json`. Connect each window to a **different** COM port.
The same COM remains exclusive (second connect gets port busy). Reloading one
window must not kill the other’s sidecar.

On WSL, serial and esp32 flash use **Windows Python** so `COM` ports work.
Install host packages on that interpreter: `mpremote`, and **`circup`** for
CircuitPython library installs (`python.exe -m pip install mpremote circup`).

```bash
chmod +x scripts/mpftp
./scripts/mpftp status          # rpc up? connected device? runtime?
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

Connect **interrupts** any running program and enters raw REPL. Runtime is
detected from `sys.implementation.name` and returned as `runtime`
(`micropython` | `circuitpython`).

| Runtime | `soft-reset` | `soft-reboot` |
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
./scripts/mpftp mkdir /lib
./scripts/mpftp rm /junk.py
./scripts/mpftp eval '1+1'
./scripts/mpftp exec 'print(1)'             # waits for EOF; use --no-follow for loops
./scripts/mpftp run ./app.py                # default --no-follow (UI-safe)
./scripts/mpftp run ./short.py --follow     # wait for script to finish
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

**Rules of thumb**

- Debug with `exec` / `eval` / `run` before rewriting `main.py` / `code.py`.
- Soft-reset after bad imports; hard-reset if the port is wedged.
- Dotfiles / `__pycache__` are skipped by the File Transfer UI; CLI `put` of a
  single path does what you ask.
- Do not put secrets in scripts that will show up in `activity.log`.

---

## Firmware: diagnose, download, build, flash

Firmware commands are **host-side** and **MicroPython-only**. They do not
hold the serial lock for the whole build. CircuitPython firmware is out of scope
for mpftp.
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

### ESP32 partition autosize

If an esp32 build fails because the app image is larger than the `factory` (or
other app) partition, mpftp **parses the overflow**, writes a grown table to
`<workspace>/esp32_partitions/<board>.csv` (sibling of `micropython/` — the
MicroPython tree is never edited), patches the build-dir `sdkconfig`, and
**rebuilds once**. Disable with `--no-autosize`.

Details: [user guide — Autosize](user-guide.md#esp32-partition-autosize).

---

## Troubleshooting playbook

| Symptom | Agent action |
|---------|----------------|
| Port busy / exclusive lock | Another mpftp window may own that COM — disconnect there, or pick the other board. Also close Thonny/serial monitors. Do not expect two windows on one COM |
| Two windows / two boards | Use distinct COM ports; run CLI from each workspace cwd (`workspace-rpc.json`). Check `mpftp status` → distinct `rpc` + `session_id` |
| `Access is denied` / `transport_dead` after hung `exec`/`run` | Sidecar should release the COM handle automatically; `disconnect` then `resume`/`connect`. If still busy: reload extension window, then replug USB only as last resort ([mpftp#3](https://github.com/bdbarnett/mpftp/issues/3)) |
| `timeout waiting for first EOF` | Board still running (UI loop). Use `run` without `--follow` / `exec --no-follow`, then `interrupt` or `soft-reset` |
| Soft-reset left UI dead after deploy | Expected: soft-reset skips `main.py`. Use `soft-reboot` or `hard-reset` to run startup |
| Dual USB (UART + native CDC) | `mpftp ports` shows `role` (`repl` vs `cdc_debug`); control on UART, `debug-tee` on CDC |
| `could not enter raw repl` after flash | Detect; erase + reflash MicroPython; corrupt FS boot loops block soft-reset |
| Wrong board / no Wi-Fi on P4 | Detect + MicroPython hints; pick `C5_WIFI` / `C6_WIFI` explicitly if needed |
| Build: required tree not found | Symlink under firmware workspace or set env (`IDF_PATH`, `EMSDK`, …); Locate… in UI |
| App partition too small | Let autosize rebuild once, or adjust `esp32_partitions/<board>.csv` |
| Module missing from firmware | See [aggregator.md](aggregator.md); `firmware cmods` |

---

## RPC reminder

```bash
./scripts/mpftp rpc ping
./scripts/mpftp rpc fs_listdir '{"path":"/"}'
# Firmware methods are also exposed over the same agent RPC when the extension runs.
```

See [user-guide.md](user-guide.md), [aggregator.md](aggregator.md), and
[developers-guide.md](developers-guide.md). Keep this file aligned when
CLI or discovery contracts change.
