# mcuctl Claude Code plugin

Installs the board-tools skill, which teaches Claude Code to drive a
MicroPython or CircuitPython board with the `mcuctl` CLI: file transfer,
REPL and exec, `mcuctl hold` for a REPL that stays open across calls,
non-interrupting capture, and firmware build and flash. The skill is short
and points at the [agent guide](../../docs/guides/drive-a-board-from-an-agent.md) for everything
else.

## Install

Install `mcuctl` so `mcuctl` is on `PATH`
(`pip install --pre mcuctl`), then:

```
/plugin marketplace add PyDevices/mcuctl
/plugin install mcuctl@mcuctl
```

For local development, point Claude Code at this repository's checkout
instead: `/plugin marketplace add /path/to/mcuctl`.

No `/plugin` command? Some Claude Code hosts, such as the Claude Desktop
app's Code panel, have the same flow in the GUI: Settings → Plugins →
**Add** → **Add from a repository**, enter `PyDevices/mcuctl`, then install
`mcuctl`.

## What's here

| Path | Role |
|---|---|
| [`.claude-plugin/plugin.json`](.claude-plugin/plugin.json) | Plugin manifest; its version tracks the repository's `VERSION` ([`scripts/check_versions.py`](../../scripts/check_versions.py) checks it) |
| [`skills/board-tools/SKILL.md`](skills/board-tools/SKILL.md) | The skill |
