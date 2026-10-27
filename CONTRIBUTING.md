# Contributing to mpftp

mpftp is a TypeScript VS Code extension, a Python package that holds the
serial session, the firmware engine and the command line, and a browser app.
A change to behavior usually lands in the Python package first. A unit test in
`cli/tests/` or a documentation fix is a good first contribution. How the parts
fit together is in [How mpftp works](docs/how-it-works/index.md).

## Where things are

| Path | What it holds |
|---|---|
| `extension/src/` | The extension host: bridge, panels, terminal, firmware panel and the session server for the command line |
| `extension/media/` | Webview HTML, JS and CSS. `ftp.html`, `ftp.js` and `ftp.css` are the File Transfer view |
| `extension/resources/templates/` | Workspace stub `micropython.cmake` and `manifest-micropython.py` |
| `extension/python/mpftp/` | A vendored copy of `cli/src/mpftp/`, staged by `scripts/stage-vendored-python.sh` so `vsce` can package it. Never edit it; edit `cli/src/mpftp/` |
| `cli/src/mpftp/` | The `pydevices-mpftp` package: `sidecar.py` (serial session), `cli.py`, `firmware.py` (discover, build, flash, detect), `firmware_build.py` (runs `build_mp.py`), `firmware_download.py` (the official catalog), `pwa.py` (the local web app), `webrepl.py`, `ble.py`, `hold.py`, `config.py`, `uf2.py` |
| `cli/src/mpftp/webui/` | The built browser app. It is committed, not ignored: the release pipeline only runs `python -m build .` with no Node step, so it has to be current on `main`. CI's `ui` job rebuilds it and runs `git diff --exit-code`, so a stale commit fails CI |
| `cli/tests/` | The Python tests (`unittest`) |
| `ui/` | The browser app's source: TypeScript with esbuild, `@xterm/xterm` and CodeMirror 6. Its File Transfer panel is the extension's own, copied in by `npm run build`, which stages everything into `cli/src/mpftp/webui/`. `src/host.ts` gives `ftp.js` an `acquireVsCodeApi()` that talks to `cli/src/mpftp/panel.py`, the Python port of the extension's message handling, so a change to one host's behavior belongs in the other too |
| `desktop/` | The desktop app, a Tauri 2 window around the browser app that starts a bundled Python running `cli/src/mpftp/desktop_host.py`. `emulator/` is the simulated board, reached as `emu://`. See [desktop/README.md](desktop/README.md) |
| `tools/` | `test_cli_workflows.py` (a workflow test that needs an attached board), `ble_bench.py` and `ble_gate.py` (Bluetooth), and `windows/` (the USB-restart task) |
| `scripts/` | The `mpftp` launcher, version and release scripts, `install-cursor-wsl.sh`, `stage-vendored-python.sh` |
| `integrations/` | The Claude Code plugin: a skill that teaches agents the CLI |
| `docs/` | The documentation site's source |

The extension id is `pydevices.mpftp`. [AGENTS.md](AGENTS.md) is the short
command list for coding agents working in this repository.

## Build, lint and test

The extension commands run from `extension/`, not the repository root:

```bash
cd extension
npm install
npm run compile          # tsc
npm run lint             # tsc --noEmit
npm test                 # extension unit tests (node:test)
npm run test:python      # unittest under cli/tests
npm run package          # VSIX via @vscode/vsce
```

`python3 tools/test_cli_workflows.py` runs the CLI end to end against attached
boards; it skips flashing unless you pass `--install-latest-firmware`.

On native Linux, serial needs nothing extra: `npm run package` bundles mpremote
and pyserial (the hashed pins in `extension/vendor-requirements.txt`) into
`extension/python/_vendor`, and the launcher puts them on the session's path.
For a WSL window that runs the extension remotely:

```bash
./scripts/install-cursor-wsl.sh     # runs npm install and compile in extension/
# then: Developer: Reload Window
```

To build the Python package without publishing it:

```bash
python -m build .                    # sdist and wheel into dist/
./scripts/package-vsix.sh            # or: npm run package, from extension/
python scripts/check_versions.py     # every version site agrees with VERSION
```

## Build the docs

```bash
pip install -r docs/requirements.txt
sphinx-build -W --keep-going -b html docs _site
```

`-W` fails on any warning, as Read the Docs does.

## Style

Keep comments short and make them say why a constraint exists (discovery order,
the WSL Python choice, soft reset versus a corrupt filesystem). Don't narrate
code that reads fine without it. The extension's vendored Python is staged from
`cli/src/mpftp/`, so edit the package, then run the packaging step.

## Run against a live board

Prefer the editor's existing session to a second serial client. The command line
finds it by itself when you run from the workspace folder; see
[Drive a board from an agent](docs/guides/drive-a-board-from-an-agent.md#how-commands-find-a-session).

## Release

One root [`VERSION`](VERSION) becomes a published
[`pydevices-mpftp`](https://test.pypi.org/project/pydevices-mpftp/) on TestPyPI
and a versioned VSIX on [GitHub Releases](https://github.com/PyDevices/mpftp/releases)
(and optionally the VS Marketplace and Open VSX), both from the same tag.

```text
Actions → "Prepare release" (prepare-release.yml, version X.Y.Z)
           │  bot opens PR "Release X.Y.Z" on release/vX.Y.Z:
           │  VERSION + a commit list in CHANGELOG.md
           ▼
you finish the PR (below), CI green, merge
           │
           ▼
tag-release.yml  (push to main touching VERSION)
           │  creates tag vX.Y.Z + GitHub Release with the App token,
           │  so the release: published event fires
           ├──▶ publish-release-packages.yml
           │      reusable-publish-release-packages.yml@publishing-v6
           │      build sdist/wheel → smoke-test install → TestPyPI
           │
           └──▶ publish-vsix.yml
                  confirm tag == VERSION → npm ci → vsce package
                  → attach *.vsix to the vX.Y.Z release
                  (optional, finals only) vsce / ovsx publish when secrets are set
```

`VERSION` is the single source of truth, not the tag. Every other version site
is committed equal to it, and `scripts/check_versions.py` enforces that in CI on
every pull request. Both publish workflows only confirm the tag they were
triggered from agrees with the `VERSION` on the commit they checked out.

Nobody creates the tag or the release by hand. Merging the release PR does it.
A release made with the default `GITHUB_TOKEN` would not fire `release` events,
so `tag-release.yml` uses the organization's release App.

### Version numbers

One version covers the PyPI distribution, the `mpftp` import, the VS Code
extension, the web UI and the Claude plugins. A person chooses it. To see what
the next patch would be:

```bash
./scripts/next_release_version.sh --verbose
```

A release can be a pre-release: `X.Y.ZaN`, `X.Y.ZbN`, `X.Y.ZrcN` or
`X.Y.Z.devN`, written in [PEP 440](https://peps.python.org/pep-0440/) form in
`VERSION`, in the wheel's name and in the tag (`v0.1.0rc1`). npm and the plugin
manifests only take SemVer, so they spell the same version the SemVer way
(`0.1.0-rc.1`, `0.1.0-dev.2`), and so does the `.vsix` file name.
`check_versions.py` knows the mapping. A pre-release is marked as one on GitHub,
goes to TestPyPI, and has its `.vsix` on the GitHub Release. It is never
published to the VS Marketplace or Open VSX, whose versions can't be republished.

### Cut a release

```bash
gh workflow run prepare-release.yml -f version=X.Y.Z   # or e.g. 0.1.0rc1
gh pr list --state open          # the bot's "Release X.Y.Z" PR
```

The bot opens the PR in two steps. It sets `VERSION` and prepends a commit list
to `CHANGELOG.md`, then a second commit runs
`python scripts/check_versions.py --write`, which sets every other version site
from `VERSION` in the form each takes: `extension/package.json`,
`ui/package.json`, the root entry of both `package-lock.json` files,
`.claude-plugin/marketplace.json` and
`integrations/claude-code-plugin/.claude-plugin/plugin.json`. If the second
commit is missing (the workflow's `sync-versions` job failed), run that command
on the `release/vX.Y.Z` branch and push.

Before merging, on the `release/vX.Y.Z` branch, turn the curated `## Unreleased`
notes in `CHANGELOG.md` into the `## vX.Y.Z (date)` section and drop the bot's
commit list, so there is one heading per release. Push any edit, wait for Tests
to pass on the PR, and merge it. Then watch:

```bash
gh run list --workflow tag-release.yml --limit 1
gh run list --workflow publish-release-packages.yml --limit 1
gh run list --workflow publish-vsix.yml --limit 1
```

A tag that fails its publish gates is burned: tags can't be moved or deleted,
so the fix ships under a new version. TestPyPI can take a few minutes to serve a
new upload, so retry an install before calling it missing.

### Secrets

| Secret | Purpose |
|---|---|
| `TESTPYPI_API_TOKEN` | Publish `pydevices-mpftp` to TestPyPI (an organization secret, inherited with `secrets: inherit`) |
| `VSCE_PAT` | Optional: publish the extension to the VS Marketplace (`vsce publish`) |
| `OVSX_PAT` | Optional: publish the extension to Open VSX (`ovsx publish`) |

No Trusted Publisher (OIDC) is configured, so publishing uses
`TESTPYPI_API_TOKEN` with `user: __token__`, like every other PyDevices
repository. Without `VSCE_PAT` and `OVSX_PAT`, `publish-vsix.yml` still attaches
the `.vsix` to the GitHub Release for "Install from VSIX…"; those two secrets
only gate the marketplace steps.
