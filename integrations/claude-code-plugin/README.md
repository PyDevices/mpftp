# mpftp Claude Code plugin

Installs the board-tools skill, which teaches Claude Code to drive a
MicroPython or CircuitPython board with the `mpftp` CLI: file transfer,
REPL and exec, `mpftp hold` for a REPL that stays open across calls,
non-interrupting capture, and firmware build and flash. The skill is short
and points at the [agent guide](../../docs/agent-guide.md) for everything
else.

## Install

Install `pydevices-mpftp` so `mpftp` is on `PATH`
(`pip install -i https://test.pypi.org/simple/ pydevices-mpftp`), then:

```
/plugin marketplace add PyDevices/mpftp
/plugin install mpftp@mpftp
```

For local development, point Claude Code at this repository's checkout
instead: `/plugin marketplace add /path/to/mpftp`.

No `/plugin` command? Some Claude Code hosts, such as the Claude Desktop
app's Code panel, have the same flow in the GUI: Settings → Plugins →
**Add** → **Add from a repository**, enter `PyDevices/mpftp`, then install
`mpftp`.

## What's here

| Path | Role |
|---|---|
| `.claude-plugin/plugin.json` | Plugin manifest; its version tracks the repository's `VERSION` (`scripts/check_versions.py` checks it) |
| `skills/board-tools/SKILL.md` | The skill |
