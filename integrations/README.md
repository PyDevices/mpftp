# Agent integrations

Coding agents drive boards with the `mcuctl` CLI. The
[agent guide](../docs/guides/drive-a-board-from-an-agent.md) is the playbook: connecting, file
transfer, the REPL, `mcuctl hold` for a REPL that stays open across calls,
firmware, and what to do when a port wedges.

For Claude Code, the [plugin](claude-code-plugin/) installs a short skill that
teaches the CLI and points at the guide:

```
/plugin marketplace add PyDevices/mcuctl
/plugin install mcuctl@mcuctl
```

Codex's Plugins panel reads the same plugin (**Plugins → Add → Add plugin
marketplace**, source `PyDevices/mcuctl`). Codex and other agents can also
read [the agent guide](../docs/guides/drive-a-board-from-an-agent.md) directly, for example from
your project's `AGENTS.md`.
