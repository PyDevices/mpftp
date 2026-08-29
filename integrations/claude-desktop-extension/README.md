# mpftp Claude Desktop extension

Wires the `mpftp` MCP server (`mpftp-mcp`) into the **Claude Desktop app**
(not the `claude` CLI) via its local Extensions system (MCPB — MCP Bundle,
née "Desktop Extensions"/DXT). This is a separate distribution mechanism
from [`../claude-code-plugin/`](../claude-code-plugin/) — the Desktop app's
plugin/marketplace panel and its Connectors panel (remote HTTPS MCP servers
only) don't work for a local stdio server like this one; Extensions does.

## Install

Requires `pydevices-mpftp` installed
(`pip install -i https://test.pypi.org/simple/ pydevices-mpftp`) so
`mpftp-mcp` resolves on `PATH`. In the published 0.0.3 package the MCP
server is not yet included — run from a clone until the next release.

1. Claude Desktop → Settings → Extensions → **Advanced settings**.
2. Under **Extension Developer**, click **Install Unpacked Extension**.
3. Point it at this directory (`integrations/claude-desktop-extension/`).

No packaging step — Claude Desktop reads `manifest.json` directly from the
directory. `server.type: "binary"` + `entry_point: "mpftp-mcp"` means the
app resolves the executable on `PATH` itself (appending `.exe` on Windows
automatically — don't put `.exe` in the manifest).

## Using it — and the one thing that will look broken if you miss it

**Local session required.** Extensions/Connectors only attach to chats
started as a **Local** session (the environment picker under the compose
box's "+" menu / a new-task dialog — options seen there: Local, Cloud,
Remote Control, WSL, SSH). A **Cloud** chat, and — confirmed by testing —
a **WSL** session too, both show "Connectors" and "Plugins" greyed out
with *"Only available in local sessions"* in that same "+" menu, and an
otherwise-correctly-installed, correctly-"Connected" extension will show
**zero** tools there. This is easy to misdiagnose as a broken manifest or
a server that failed to start — check the session type first. Only
**Local** (native Windows/Mac, not WSL) was confirmed working.

Once you're in a Local session, the tools show up automatically — no
per-chat enabling needed. Confirmed working end to end: a fresh Local
chat listed all 25 tools (exposed as `mcp__mpftp__*` /
`mcp__plugin_mpftp_mpftp__*`) and `list_ports` returned real hardware
(two COM ports, correct VID:PID/roles) on the first call.

If a chat *is* a Local session and tools still don't show up:
- Settings → Connectors → confirm `mpftp` shows **Connected** (green check).
- Settings → Plugins → Mpftp → **Manage** → confirm the toggle is on and
  the Connectors tab there also shows `mpftp`.
- Start a brand-new chat after fixing either of the above — an
  already-open chat doesn't pick up a newly-connected server retroactively.

## What's here

| Path | Role |
|---|---|
| `manifest.json` | MCPB manifest — declares the `mpftp-mcp` binary as the server, no bundled entry point |
