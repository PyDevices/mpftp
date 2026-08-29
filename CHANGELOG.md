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

