# mpftp Claude Code plugin

Bundles the `mpftp` MCP server (`mpftp-mcp`, i.e. `python -m mpftp.mcp`) and a
board-tools skill so Claude Code can drive a connected MicroPython/
CircuitPython board directly — filesystem transfer, REPL/exec, a
non-interrupting `watch_repl`/`probe` pair for long-running scripts, and
firmware build/flash.

## Install

Requires `pydevices-mpftp` on `PATH` (`pip install pydevices-mpftp`) —
`mpftp-mcp` is one of its console scripts.

```
/plugin marketplace add PyDevices/mpftp
/plugin install mpftp@mpftp
```

Or point Claude Code at this directory directly for local development:

```
/plugin marketplace add /path/to/mpftp/integrations
```

**No `/plugin` command?** Some Claude Code hosts (e.g. the Claude Desktop
app's embedded Claude Code panel) don't expose slash commands but have the
same functionality as a GUI flow: Settings → Plugins → **Add** →
**Add from a repository**, enter `PyDevices/mpftp`, then install `mpftp`
from the marketplace that appears. This is the same underlying mechanism,
just a different front end for it.

**Local session required.** Whichever way you install it, the plugin's
tools only attach to chats running as a **Local** session — check the
environment picker (Local/Cloud/Remote Control/WSL/SSH) wherever new
chats/tasks get created. A Cloud or (confirmed by testing) WSL session
shows the plugin as installed and "Connected" but exposes **zero** tools
to the model — that's a session-type gap, not a broken install. Verify by
checking the "+" / attachment menu near the compose box: if "Connectors"
and "Plugins" there say "Only available in local sessions," start a new
chat in a Local session instead.

## What's here

| Path | Role |
|---|---|
| `.claude-plugin/plugin.json` | Plugin manifest (name, version — kept in sync with the repo's `VERSION` by `scripts/check_versions.py`) |
| `.mcp.json` | Declares the `mpftp` stdio MCP server (`mpftp-mcp`) |
| `skills/board-tools/SKILL.md` | Condensed board-workflow guidance (raw REPL interrupts, soft-reset vs soft-reboot, CircuitPython specifics) |

The MCP server itself (`mpftp.mcp`, 25 tools) lives in the main package at
[`cli/src/mpftp/mcp.py`](../../cli/src/mpftp/mcp.py) — this plugin is a thin
wrapper that tells Claude Code how to start it and when to reach for it, not
a separate implementation.
