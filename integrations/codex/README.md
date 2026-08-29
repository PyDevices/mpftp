# mpftp for Codex

Codex has two integration points, and both were confirmed working
end-to-end (connect, filesystem, and REPL against a real board) —
**no equivalent of the Claude Desktop app's "Local session required"
restriction**; it just worked in a normal Codex chat.

## Option A — Plugins panel (Codex app, GUI)

Codex's Plugins panel reads the **same** plugin files this repo already
ships for Claude Code — no separate Codex-specific package:

1. Codex app → **Plugins** (left sidebar) → **Add** → **Add plugin marketplace**.
2. **Source**: `PyDevices/mpftp`. Leave **Git ref** as `main` and
   **Sparse paths** empty — the repo root's `.claude-plugin/marketplace.json`
   already points at [`../claude-code-plugin/`](../claude-code-plugin/), the
   same directory Claude Code installs from.
3. Install the `mpftp` plugin from the marketplace that appears. It shows
   up with **MCP servers: 1** (`mpftp`) and **Skills: 1** (`Board Tools`).

Requires `pydevices-mpftp` installed
(`pip install -i https://test.pypi.org/simple/ pydevices-mpftp`) so
`mpftp-mcp` resolves on `PATH` wherever Codex spawns the server. In the
published 0.0.3 package the MCP server is not yet included — run from a
clone until the next release.

## Option B — bare `codex` CLI, no GUI

For headless `codex` CLI use (no Plugins panel available), register the
MCP server directly in its config file instead:

```toml
[mcp_servers.mpftp]
command = "mpftp-mcp"
```

in `~/.codex/config.toml` (or a project-scoped `.codex/config.toml`), or:

```bash
codex mcp add mpftp -- mpftp-mcp
```

This path has no bundled skill — see
[`docs/agent-guide.md`](../../docs/agent-guide.md) via `AGENTS.md` instead
(also covered below).

`mpftp-mcp` needs to be the same interpreter mpftp's own CLI uses — on
WSL, the one that can see your board's `COM` port (usually Windows
`python.exe`), the same choice `mpftp.pythonPath` makes for the VS Code
extension.

## Tools

25 typed tools covering device connect, filesystem, REPL/exec, `probe`
(run → wait → capture without leaving a script's raw-REPL session open
indefinitely), and firmware build/flash — the same operations as the `mpftp`
CLI, over the same session. Ask Codex to list tools from the `mpftp` server,
or see [`../claude-code-plugin/`](../claude-code-plugin/) for the same tool
list with descriptions (the two integrations share one server).

## Board workflow context

Point Codex at [`docs/agent-guide.md`](../../docs/agent-guide.md) (e.g. via
`AGENTS.md`) for the non-obvious parts of driving a real board: raw REPL
interrupts whatever's running, soft-reset skips `main.py`, CircuitPython
prefers the mounted CIRCUITPY drive, and so on — the MCP tool schemas cover
*what* each tool does, not *when* a board-flashing agent should reach for one
over another.
