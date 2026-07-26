---
name: mpftp
description: >-
  Drive MicroPython and CircuitPython boards via mpftp (mpremote-backed):
  connect, filesystem, REPL, resets, mip/circup, romfs (MP-only), and watch live
  activity. Use when working with mpftp, MicroPython/CircuitPython serial boards,
  COM ports on WSL, or board file transfer/REPL.
---

# mpftp — agent guide

mpftp is a Cursor/VS Code extension plus CLI for MicroPython and CircuitPython
over USB serial. Prefer the **CLI through the extension TCP RPC**
(`mpftp status` / `~/.mpftp/rpc.port`) so you share the UI session and do not
fight over the serial port.

**Firmware** download/build/flash is MicroPython-only.

## Paths (home only — never written into the repo)

| Path | Purpose |
|------|---------|
| `~/.mpftp/rpc.port` | Live Agent RPC address (usually `127.0.0.1:7430`) |
| `~/.mpftp/activity.log` | NDJSON of connects, transfers, RPC, errors |
| `~/.mpftp/repl.log` | Mirrored REPL I/O when REPL is open |

Override with `MPFTP_RPC=127.0.0.1:<port>` when several Cursor windows compete.
Extension must be loaded (window open with mpftp installed) for the socket.

## Setup tooling

```bash
chmod +x scripts/mpftp
./scripts/mpftp status
ln -sf "$(pwd)/scripts/mpftp" ~/bin/mpftp
```

On WSL, the CLI uses Windows `python.exe` + mpremote for COM ports (same as the UI).
For CircuitPython packages also install circup on that interpreter:

```bash
python.exe -m pip install mpremote circup
```

## Watch activity

```bash
./scripts/mpftp watch              # activity.log
./scripts/mpftp watch --repl       # repl.log
tail -f ~/.mpftp/activity.log
```

## Board ops

```bash
./scripts/mpftp ports
./scripts/mpftp connect COM4       # interrupt + runtime-aware clean + RTC
./scripts/mpftp resume             # reconnect last device
./scripts/mpftp status             # includes runtime when connected
./scripts/mpftp ls /
./scripts/mpftp tree /
./scripts/mpftp put ./main.py /main.py --verify
./scripts/mpftp get /main.py ./main.py --verify
./scripts/mpftp cp ./lib :/lib --verify          # : = board path
./scripts/mpftp cp :/a.py :/b.py
./scripts/mpftp hash /main.py
./scripts/mpftp mkdir /lib
./scripts/mpftp touch /boot.py
./scripts/mpftp rename /old.py /new.py
./scripts/mpftp rm /junk.py
./scripts/mpftp rm -r /olddir
./scripts/mpftp edit /main.py      # requires $EDITOR
./scripts/mpftp eval '1+1'
./scripts/mpftp exec 'print(42)'
./scripts/mpftp run ./script.py
./scripts/mpftp interrupt              # Ctrl-C; no reset
./scripts/mpftp soft-reset             # MP: skip main.py; CP: friendly↔raw
./scripts/mpftp hard-reset
./scripts/mpftp bootloader
./scripts/mpftp rtc
./scripts/mpftp rtc --set
./scripts/mpftp df
./scripts/mpftp mip github:org/repo            # MicroPython only
./scripts/mpftp circup adafruit_display_text   # CircuitPython only → /lib
./scripts/mpftp mount /some/local/path         # MicroPython only
./scripts/mpftp umount
./scripts/mpftp romfs query                    # MicroPython only
./scripts/mpftp romfs build ./romdir -o ./out.romfs
./scripts/mpftp romfs deploy ./out.romfs --partition 0
./scripts/mpftp disconnect
```

### Runtime notes

| | MicroPython | CircuitPython |
|---|---|---|
| Soft reset | Raw soft-reset (no `main.py`) | Friendly↔raw (no Ctrl-D / `code.py`) |
| Packages | `mip` | `circup` (host stage + serial put) |
| mount / romfs | yes | no |

Raw RPC:

```bash
./scripts/mpftp rpc fs_listdir '{"path":"/"}'
./scripts/mpftp rpc ping
```

Standalone (no extension): pass `-d/--device` after the subcommand.

## Firmware builder (host-side build & flash)

Builds **MicroPython** from a local checkout (no private CircuitPython firmware
tooling in mpftp). User C modules (`micropython.cmake` / `*/micropython.mk`) and
a frozen `manifest.py` are auto-discovered from the checkout's **parent**
workspace. mpftp aggregates **every** workspace module for **every** port and
never gates by compatibility; each module opts in/out (and owns its deps, e.g.
SDL2 for `usdl2`) via its own `micropython.mk` / `micropython.cmake`. Runs as its
own process (native Linux python on WSL for `make`), so it never blocks the
serial session. All MP ports build; flash is supported for `esp32`, `rp2`,
`samd`.

```bash
./scripts/mpftp firmware discover                 # resolved MP tree + workspace + host
./scripts/mpftp firmware list                     # ports -> boards -> variants tree
./scripts/mpftp firmware cmods                    # discovered user C modules
./scripts/mpftp firmware build --port esp32 --board ESP32_GENERIC   # streams log
./scripts/mpftp firmware build --port unix --variant standard --clean
./scripts/mpftp firmware artifact --port esp32 --board ESP32_GENERIC # Ready?/path
./scripts/mpftp firmware flash --port esp32 --board ESP32_GENERIC -d COM4
./scripts/mpftp firmware flash -d COM5            # same artifact, next board (no rebuild)
./scripts/mpftp firmware detect -d COM4           # esptool-first chip/flash/security probe
./scripts/mpftp firmware partitions get --board ESP32_GENERIC
./scripts/mpftp firmware partitions candidates --board ESP32_GENERIC  # stock+override tables
./scripts/mpftp firmware partitions split --board ESP32_GENERIC --storage-bytes 4194304 --flash-mb 8
./scripts/mpftp firmware partitions set --board ESP32_GENERIC --rows '[{...}]'
./scripts/mpftp firmware partitions reset --board ESP32_GENERIC
```

- `--mp` is auto-discovered (setting `mpftp.micropythonPath` → `MP_DIR` →
  common layouts). Pass `--mp PATH` to override.
- **Toolchains resolve at build time, not at panel open.** When **Build** runs,
  mpftp resolves the port's toolchain — ESP-IDF (esp32), emsdk (webassembly), or
  a cross-gcc (windows→mingw-w64, esp8266→xtensa-lx106, arm ports→arm-none-eabi,
  riscv qemu→riscv64, pic16bit→xc16, renesas-ra→arm+protoc-c). If one is missing
  the build returns a structured `needToolchain` and the panel prompts to
  **Locate…** it (saved to `mpftp.idfPath`/`mpftp.emsdkPath` or
  `mpftp.toolchainBins`, then the build retries) or open **Install
  instructions**. esp32 also validates the ESP-IDF version against the port's
  supported list. `discover` no longer probes toolchains.
- windows builds cross-compile with `CROSS_COMPILE=x86_64-w64-mingw32-`;
  webassembly relaxes the port's `-Werror` via `EMCC_CFLAGS` (no upstream edit).
  Flashing on WSL uses the Windows python esptool so it can see `COMx`.
- rp2/samd flash copies the `.uf2` to the bootloader drive (rp2 falls back to
  `picotool`); put the board in BOOTSEL/bootloader mode first.
- **Detect** (Firmware page **Detect** button / `firmware detect`) is
  **esptool-first**: it reads chip / revision / flash size / PSRAM / security
  directly from the ROM bootloader, so it works on a bare board with no
  MicroPython. If a MicroPython session was active it is briefly released,
  probed, then reconnected to enrich the card (freq, heap, `_build`). It
  auto-selects board / variant / flash size; non-Espressif boards only get a
  suggested firmware port, never a forced `ESP32_GENERIC_*`. Flash encryption /
  secure boot enabled → a pre-flash guard warns before writing.
- **ESP32-P4 Wi-Fi variants** (`C5_WIFI` / `C6_WIFI`) use an external radio that
  esptool cannot see; they are chosen only from MicroPython `_build`/machine
  hints or an explicit user pick.
- ESP32 partition edits save to `<workspace>/esp32_partitions/<board>.csv` (a
  sibling of the micropython tree — the MicroPython clone is never modified) and
  are injected into the **build-dir** `sdkconfig` at build time, referenced
  *relative* to `ports/esp32` (`../../../esp32_partitions/<board>.csv`), with a
  companion `<board>.sdkconfig` fragment for the flash size. The Firmware page
  has no manual partition/storage controls — sizing is automatic (see Autosize);
  the `partitions` CLI subcommands below remain for scripted/manual overrides.
- **Autosize:** if a build overflows the app partition, mpftp parses the ESP-IDF
  `app partition is too small … (overflow …)` error, grows the app partition to
  fit (into `esp32_partitions/<board>.csv`), and rebuilds once. `--no-autosize`
  disables it.
- UI: **Firmware** button in the File Transfer toolbar, or `mpftp: Build & Flash
  Firmware`. Same operations over Agent RPC as `firmware_*` methods
  (`firmware_list`, `firmware_build`, `firmware_flash`, `firmware_partitions`, …).

## Rules

- Prefer the TCP RPC session over spawning a second sidecar while the UI is connected.
- Connect fails clearly if the port is bootloader/UF2-only (no MicroPython/CircuitPython raw REPL).
- Dotfiles / `__pycache__` / `*.pyc` are skipped by the FTP UI upload; CLI `put` of a single file does what you ask.
- Do not commit secrets into activity logs; they may contain paths and script snippets.
