## Unreleased

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

