# mpftp for Codex CLI

[Codex](https://developers.openai.com/codex) doesn't have an installable plugin
system — MCP servers are registered directly in its config file. Add the mpftp
server to `~/.codex/config.toml` (or a project-scoped `.codex/config.toml`):

```toml
[mcp_servers.mpftp]
command = "mpftp-mcp"
```

Or via the CLI:

```bash
codex mcp add mpftp -- mpftp-mcp
```

`mpftp-mcp` is the console script `pip install pydevices-mpftp` puts on
`PATH` (`python -m mpftp.mcp` also works if you'd rather pin an explicit
interpreter). It needs to be the same interpreter mpftp's own CLI uses — on
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
