# mpftp agent integrations

Both integrations here front the same `mpftp.mcp` stdio MCP server (in the
main package, [`cli/src/mpftp/mcp.py`](../cli/src/mpftp/mcp.py)) — 25 typed
tools covering device connect, filesystem transfer, REPL/exec, a
non-interrupting `watch_repl`/`probe` pair for long-running board scripts,
and firmware build/flash. Neither integration reimplements any of that; they
only wire an agent host up to the server and, for Claude Code, add
condensed board-workflow guidance as a skill.

| | Claude Code | Codex CLI |
|---|---|---|
| Directory | [`claude-code-plugin/`](claude-code-plugin/) | [`codex/`](codex/) |
| Distribution | Installable plugin (`.claude-plugin/plugin.json`, `.mcp.json`, a skill) | No plugin system — a `~/.codex/config.toml` snippet |
| Board-workflow guidance | Bundled skill (`skills/board-tools/SKILL.md`) | Point `AGENTS.md` at `docs/agent-guide.md` |

Requires `pydevices-mpftp` installed (`pip install pydevices-mpftp`) — both
integrations run its `mpftp-mcp` console script.

## Versioning

The Claude Code plugin's `.claude-plugin/plugin.json` `version` (and the
root `.claude-plugin/marketplace.json` entry) track the repository's root
`VERSION`, checked by `scripts/check_versions.py` alongside the Python
package and VS Code extension. There's nothing to bump for Codex — it's a
config snippet, not a versioned artifact.
