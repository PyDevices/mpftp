# mpftp agent integrations

All three integrations here front the same `mpftp.mcp` stdio MCP server (in
the main package, [`cli/src/mpftp/mcp.py`](../cli/src/mpftp/mcp.py)) — 25
typed tools covering device connect, filesystem transfer, REPL/exec, a
non-interrupting `watch_repl`/`probe` pair for long-running board scripts,
and firmware build/flash. None of them reimplement any of that; they only
wire an agent host up to the server and, for Claude Code, add condensed
board-workflow guidance as a skill.

| | Claude Code (CLI) | Claude Desktop app | Codex (app, Plugins panel) | Codex (bare CLI) |
|---|---|---|---|---|
| Directory | [`claude-code-plugin/`](claude-code-plugin/) | [`claude-desktop-extension/`](claude-desktop-extension/) | same as Claude Code — [`claude-code-plugin/`](claude-code-plugin/) | [`codex/`](codex/) |
| Distribution | Installable plugin (`.claude-plugin/plugin.json`, `.mcp.json`, a skill) | MCPB extension (`manifest.json`), installed unpacked | **Same marketplace/plugin format as Claude Code** — Codex's Plugins panel reads it directly, no separate package | No plugin system — a `~/.codex/config.toml` snippet |
| Board-workflow guidance | Bundled skill (`skills/board-tools/SKILL.md`) | None yet — MCPB extensions don't carry skills | Bundled skill (same one) | Point `AGENTS.md` at `docs/agent-guide.md` |
| "Local session" gotcha | Yes — see below | Yes — see below | **No** — confirmed working in a normal Codex chat | N/A |

**These are (at least) four different install paths across two products**,
and confirmed working end to end in all four as of 2026-08-24. A plain
`claude` CLI session (with `/plugin` support) is not the same thing as the
Claude Desktop app (Settings → Extensions), which is not the same thing as
either Codex path. Confirm which one you're actually looking at before
picking a directory — though note Claude Code's plugin and Codex's Plugins
panel happen to consume the exact same files, so
[`claude-code-plugin/`](claude-code-plugin/) genuinely serves both.

Requires `pydevices-mpftp` installed (`pip install pydevices-mpftp`) — all
of them run its `mpftp-mcp` console script.

**The two Claude Desktop app paths need a "Local" session; Codex doesn't
have this restriction.** In the Claude Desktop app, MCP tools from a
plugin or extension only attach to chats started as a **Local** session —
confirmed by testing that a **Cloud** or even a **WSL** session shows the
install as complete and "Connected" but exposes no tools at all, silently.
Check the session-type picker (Local/Cloud/Remote Control/WSL/SSH,
wherever new chats/tasks get created) before assuming a broken install.
Codex was confirmed working in a normal chat with no equivalent
restriction. See the "Local session required" sections in
[`claude-code-plugin/README.md`](claude-code-plugin/README.md) and
[`claude-desktop-extension/README.md`](claude-desktop-extension/README.md)
for the exact symptoms and how to check for it.

## Versioning

The Claude Code plugin's `.claude-plugin/plugin.json` `version`, the root
`.claude-plugin/marketplace.json` entry, and the desktop extension's
`manifest.json` `version` all track the repository's root `VERSION`,
checked by `scripts/check_versions.py` alongside the Python package and VS
Code extension. There's nothing to bump for Codex — it's a config snippet,
not a versioned artifact.
