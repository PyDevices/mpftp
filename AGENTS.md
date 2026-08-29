# AGENTS.md

TypeScript VS Code extension with a Python sidecar/firmware engine.

```bash
cd extension
npm install
npm run compile          # tsc
npm run lint             # tsc --noEmit
npm run test:python      # unittest under cli/tests
npm run package          # VSIX via @vscode/vsce
```

- Prefer the extension's **TCP RPC** over opening a second serial connection —
  see [docs/agent-guide.md](docs/agent-guide.md) for the session model.
- On WSL, serial and esp32 flash need **Windows Python** (`python.exe`), not
  the WSL interpreter — see [docs/user-guide.md](docs/user-guide.md#requirements).

For board ops, CLI workflows, firmware build/flash, and the troubleshooting
playbook, see **[docs/agent-guide.md](docs/agent-guide.md)**.
