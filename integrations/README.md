# mpftp agent integrations

All three integrations here front the same `mpftp.mcp` stdio MCP server (in
the main package, [`cli/src/mpftp/mcp.py`](../cli/src/mpftp/mcp.py)) — 25
typed tools covering device connect, filesystem transfer, REPL/exec, a
non-interrupting `watch_repl`/`probe` pair for long-running board scripts,
and firmware build/flash. None of them reimplement any of that; they only
wire an agent host up to the server and, for Claude Code, add condensed
board-workflow guidance as a skill.

| | Claude Code (CLI) | Claude Desktop app | Codex CLI |
|---|---|---|---|
| Directory | [`claude-code-plugin/`](claude-code-plugin/) | [`claude-desktop-extension/`](claude-desktop-extension/) | [`codex/`](codex/) |
| Distribution | Installable plugin (`.claude-plugin/plugin.json`, `.mcp.json`, a skill) | MCPB extension (`manifest.json`), installed unpacked | No plugin system — a `~/.codex/config.toml` snippet |
| Board-workflow guidance | Bundled skill (`skills/board-tools/SKILL.md`) | None yet — MCPB extensions don't carry skills | Point `AGENTS.md` at `docs/agent-guide.md` |

**These are three different products** and none of the three install paths
covers the other two — a plain `claude` CLI session (with `/plugin` support)
is not the same thing as the Claude Desktop app (Settings → Extensions), and
neither is Codex. Confirm which one you're actually looking at before
picking a directory.

Requires `pydevices-mpftp` installed (`pip install pydevices-mpftp`) — all
three run its `mpftp-mcp` console script.

## Versioning

The Claude Code plugin's `.claude-plugin/plugin.json` `version`, the root
`.claude-plugin/marketplace.json` entry, and the desktop extension's
`manifest.json` `version` all track the repository's root `VERSION`,
checked by `scripts/check_versions.py` alongside the Python package and VS
Code extension. There's nothing to bump for Codex — it's a config snippet,
not a versioned artifact.
