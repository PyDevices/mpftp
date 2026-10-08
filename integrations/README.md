# Agent integrations

Coding agents drive boards with the `mpftp` CLI. The
[agent guide](../docs/agent-guide.md) is the playbook: connecting, file
transfer, the REPL, `mpftp hold` for a REPL that stays open across calls,
firmware, and what to do when a port wedges.

For Claude Code, the [plugin](claude-code-plugin/) installs a short skill that
teaches the CLI and points at the guide:

```
/plugin marketplace add PyDevices/mpftp
/plugin install mpftp@mpftp
```

Codex's Plugins panel reads the same plugin (**Plugins → Add → Add plugin
marketplace**, source `PyDevices/mpftp`). Codex and other agents can also
read [docs/agent-guide.md](../docs/agent-guide.md) directly, for example from
your project's `AGENTS.md`.
