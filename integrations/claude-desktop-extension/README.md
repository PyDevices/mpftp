# mpftp Claude Desktop extension

Wires the `mpftp` MCP server (`mpftp-mcp`) into the **Claude Desktop app**
(not the `claude` CLI) via its local Extensions system (MCPB — MCP Bundle,
née "Desktop Extensions"/DXT). This is a separate distribution mechanism
from [`../claude-code-plugin/`](../claude-code-plugin/) — the Desktop app's
plugin/marketplace panel and its Connectors panel (remote HTTPS MCP servers
only) don't work for a local stdio server like this one; Extensions does.

## Install

Requires `pydevices-mpftp` installed (`pip install pydevices-mpftp`) so
`mpftp-mcp` resolves on `PATH`.

1. Claude Desktop → Settings → Extensions → **Advanced settings**.
2. Under **Extension Developer**, click **Install Unpacked Extension**.
3. Point it at this directory (`integrations/claude-desktop-extension/`).

No packaging step — Claude Desktop reads `manifest.json` directly from the
directory. `server.type: "binary"` + `entry_point: "mpftp-mcp"` means the
app resolves the executable on `PATH` itself (appending `.exe` on Windows
automatically — don't put `.exe` in the manifest).

## What's here

| Path | Role |
|---|---|
| `manifest.json` | MCPB manifest — declares the `mpftp-mcp` binary as the server, no bundled entry point |
