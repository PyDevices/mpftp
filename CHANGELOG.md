## v0.0.8 (2026-09-25)

- ble: chain the original error when a drop is explained (ruff B904)
- docs: ble:// with a paired board
- ble://: first write with a response; undo a too-weak pairing
- ble://: use a paired link
- Reach a board over Bluetooth: ble:// devices through bledev

## Unreleased

- Boards can be reached over Bluetooth from the CLI and agents: every board
  command takes `-d ble://NAME`, a board running pydevices' `bledev.repl` or
  `bledev.filetransfer`. exec, run, the REPL, interrupt, ls, put and get all
  work, with no serial port involved. Files use the board's BLE file-transfer
  service (CircuitPython's protocol) when it has one, which is 19 to 35 times
  faster than the raw REPL. The password comes from `MPFTP_BLE_PASSWORD`,
  `blePassword`, `mpftp wifi password ble://NAME`, or the WebREPL one. The
  sidecar's Python needs bleak. How it works and the measurements:
  [docs/plans/ble.md](docs/plans/ble.md).

## v0.0.7 (2026-09-24)

- `interrupt` over Wi-Fi no longer calls a live board busy. Bytes left over
  from the connect hid the board's answer to Ctrl-C, and a board that says
  nothing to Ctrl-C (raw REPL, or a program that catches KeyboardInterrupt
  quietly) now gets asked for a prompt before mpftp gives up on it. The reply
  carries `latency_ms`. A Wi-Fi connect the board hangs up on before the
  password prompt is retried twice, in case the previous client is still
  closing.
- Firmware builds take a selection of modules instead of every sibling folder
  (mpftp#36). `mpftp firmware modules` lists what you can pick: repositories
  whose `manifest.py` names a C half with `c_module()`, older usermods, and
  freeze-only manifests, with the dependencies their manifests include.
  `mpftp firmware build --preset NAME --modules A,B` builds a preset plus
  extra modules; a preset is a saved selection from an overlay such as
  micropython-pydevices, whose boards and variants now appear beside
  upstream's and build with `BOARD_DIR=` / `VARIANT_DIR=`. `--build-dir`
  builds outside the shared port folder. The Firmware panel's Modules card
  has the same preset menu and a checkbox per module. `firmware list` now
  walks ports, then boards, then modules in plain text (`--json` for the
  tree). Extra folders to scan go in the new `firmwareModuleRoots` setting.
  The workspace-root aggregators (`micropython.cmake`,
  `manifest-micropython.py`) and **Create stubs…** are gone. How it works:
  [docs/firmware-modules.md](docs/firmware-modules.md).
- The agent guide has a section on reaching a board over Wi-Fi and when an
  agent would want to: [docs/agent-guide.md](docs/agent-guide.md).

## v0.0.6 (2026-09-24)

- Wi-Fi is a first-class connection in the VS Code extension, the PWA and the
  CLI. **Enable Wi-Fi access** (over USB) shows the change to `boot.py` and
  writes a marked block that joins Wi-Fi from the board's `secrets.py` and
  starts WebREPL; **Disable** puts `boot.py` back byte for byte. Boards seen
  with Wi-Fi up are remembered and offered by name, `NAME.local` resolves by
  mDNS where the network allows, and each board keeps its own password
  (SecretStorage in VS Code, a 0600 file for the CLI and PWA). Transfers turn
  Wi-Fi power save off and restore the exact prior value. A board stuck in a
  loop that never yields is reported as that, not as a timeout. `mount` is
  serial-only. The decisions and the P4 run:
  [docs/plans/wifi-webrepl.md](docs/plans/wifi-webrepl.md).

- Reach a board over Wi-Fi: every board command takes `-d ws://HOST[:8266]`
  and talks to it through WebREPL, with the password taken from
  `MPFTP_WEBREPL_PASSWORD` or `webreplPassword`. Uploads use WebREPL's binary
  PUT and downloads stream as binary frames, so a 128 KB file moves at 45-200
  KB/s against 2.7 KB/s on the P4's serial port. Serial is unchanged. Details
  and the numbers: [docs/plans/wifi-webrepl.md](docs/plans/wifi-webrepl.md).

- Stop reporting success for a board that came back broken (mpftp#34). When the
  same instance id is present after the restart and its status is not `OK`, the
  script logs Windows' Problem code and exits 6, and `mpftp usb-restart` exits
  non-zero rather than printing `"ok": false` and returning 0. The 2026-09-21
  log has the case it fixes: `pnputil /restart-device ok`, `back as "USB
  Composite Device" status=Error`, `LastTaskResult 0`, and no COM port. A node
  that is *gone* afterwards is still a success — that is a board re-enumerating
  into download mode.

- Fix the no-UAC ESP32 USB recovery, which had never worked (mpftp#31). The
  scheduled task `mpftp-restart-esp-usb` ran `Get-PnpDevice | Restart-PnpDevice`
  inline, and Windows PowerShell has no `Restart-PnpDevice` — so it failed on
  every device and reported `LastTaskResult 1`, which from outside looks exactly
  like a board that refused to come back. It cost the 2026-09-17 pin-move run
  its S3 half and the 2026-09-21 live-audio spike its auto-suspend measurement.
  `tools/windows/restart-esp-usb.ps1` is now what the task runs: one device per
  run named by instance id (the old `USB\VID_303A*` sweep would have bounced
  every Espressif board on the bench together), `pnputil /restart-device` as the
  verb, a transcript, and exit codes that separate "no such device" from "the
  restart failed". Disable+Enable is the fallback only where `pnputil` has no
  restart verb: the first installed version tried it after a 1167 ("the device
  is not connected"), the Disable stuck, and a board came back with its USB node
  disabled. The script now enables a disabled node before anything else and
  never leaves one disabled on the way out.
- Add `mpftp usb-restart` — `--status` to ask whether the recovery is really
  installed before planning around it, `--list` to find instance ids rather than
  hard-coding them, `--instance` to drive it. `--status` inspects the action the
  task is registered with, so a dead recovery reads as dead.
- Harden the request path against link-following, since a SYSTEM task reads and
  deletes inside a directory ordinary accounts write to. The request directory
  grants Users only CreateFiles on the folder plus Modify on files within it —
  no Delete on the folder, no CreateDirectories — is owned by Administrators,
  and holds an admin-only `.keep` so it can never be emptied and converted into
  a junction; the installer refuses to run onto an existing reparse point. The
  script refuses a request that is a symlink, a hard link, or sits beneath a
  reparse point, deletes nothing when it does, and does not echo the contents.
  Both conditions are needed: measured here, a hard link carries no ReparsePoint
  attribute, and a WSL symlink carries it with a blank `LinkType`.
- Add `tools/windows/install-restart-esp-usb-task.ps1`: the one elevated step.
  The task runs as SYSTEM, so the script it executes is installed where only
  administrators can write it; the one thing an unprivileged caller supplies is
  an instance id in `C:\ProgramData\mpftp\restart-esp-usb.target`, matched whole
  against an allow-list, never executed, and deleted on read.

- Add `monitor`: read-only console capture on a COM, held open for `--seconds`
  (or until Ctrl-C), streaming bytes to stdout and appending to `--log-path`.
  This is the capture `debug-tee` could not do from the CLI: the one-shot
  `debug-tee` returned immediately and its private sidecar (and the tee thread)
  died with the command, so the log always stayed empty. `monitor` keeps the
  session alive for the whole window, so the sidecar's tee loop actually
  writes. It never enters raw REPL and never toggles DTR/RTS, so a board
  autostarted from `main.py` keeps running and its `stderr` / ESP-IDF panic
  backtrace is captured — the missing piece for debugging native crashes and
  C-module `fprintf(stderr, ...)` output that never reaches a Python-side log.
- `monitor` stops the tee when the capture ends, on both transports. The tee
  runs inside the session rather than inside the socket that asked for it, so
  an RPC-mode client that simply closed its stream left the COM port held and
  the log growing until something called `debug-tee --stop`.
- Fix `SidecarClient.close()` deadlock after a streaming capture: the daemon
  stdout reader used by `stream_repl` / `stream_debug_tee` owns the pipe, so a
  graceful `disconnect` RPC in `close()` hung forever fighting it. `close()`
  now terminates the process directly once a reader is active.

## v0.0.5 (2026-09-07)

- Add board CLI workflow test; pick .bin vs .uf2 and fix put -r, romfs, mpy-cross.
- run --follow: emit captured output on timeout (mpftp#25)
- Drop the cmods repo name from mpftp's descriptive prose

## v0.0.4 (2026-08-29)

- Adopt publishing-v6 (MIP second-publication race fix)

## v0.0.4.dev1 (2026-08-29)

- Fix documentation defects found by audit
- Grant publishing-v5's permission ceiling (assets + OIDC)
- Adopt publishing-v5 and release-PR automation (Phase 1 batch 1)
- ci: bump actions/setup-python from 5 to 7 in the actions group (#22)
- docs/plans: add mpftp shell (interactive FTP-style REPL) design doc
- PWA: scope RPC responses to the requesting tab, not every tab
- mpftp clean, mip --index, and workspace-rpc.json diagnostics
- Document Codex's Plugins panel, confirmed working with no session gotcha
- Fix CI: test_pick_latest_release was silently hitting the real network
- Document the Claude integration install/usage gotchas found by testing
- Add a Claude Desktop app extension (MCPB), distinct from the CLI plugin
- Reskin the PWA to match the PyDevices simulator's shape and feel
- Add a local PWA: file transfer + REPL without an editor (mpftp#11 phase 11)
- Add mpftp.mcp server plus Claude Code / Codex integrations (mpftp#19)
- Compile .py to .mpy via mpy-cross on upload (mpftp#4)
- Add probe: run -> wait -> capture in one command
- Add watch-repl: a non-interrupting live tail of the board's own stdout
- Structured {ok, error, hint} envelope on CLI failure instead of bare text
- Verify by default, distclean fallback, WSLENV forwarding, debug-tee path fix
- Fix CI: drop the unavailable pyserial import from the new serial test
- Bound serial write timeouts and fail fast on wedged connects

