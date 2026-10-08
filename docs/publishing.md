# Publishing and releases

How one root [`VERSION`](../VERSION) becomes a published
[`pydevices-mpftp`](https://test.pypi.org/project/pydevices-mpftp/) on TestPyPI
*and* a versioned VSIX on
[GitHub Releases](https://github.com/PyDevices/mpftp/releases) (and optionally
the VS Marketplace / Open VSX) — both from the same tag, in one step.

## Pipeline

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

**`VERSION` is the single source of truth**, not the tag. Every other version
site is committed equal to it — `scripts/check_versions.py` enforces this in
CI on every pull request. Both publish workflows only confirm the tag they were
triggered from agrees with the `VERSION` on the commit they checked out.

**Nobody creates the tag or the release by hand.** Merging the release PR does
it. A release made with the default `GITHUB_TOKEN` would not fire `release`
events, so `tag-release.yml` uses the org's release App; a bare `git tag` +
`git push` starts nothing.

## Version numbers

One version covers the PyPI distribution, the `mpftp` import, the VS Code
extension, the web UI, and the Claude plugins — there is no per-component
version. The version is chosen by a person. To see what the next patch would be:

```bash
./scripts/next_release_version.sh --verbose
```

A release can be a pre-release: `X.Y.ZaN`, `X.Y.ZbN`, `X.Y.ZrcN` or
`X.Y.Z.devN`, written in [PEP 440](https://peps.python.org/pep-0440/) form
in `VERSION`, in the wheel's name and in the tag (`v0.1.0rc1`). npm and the
plugin manifests only take SemVer, so they spell the same version the SemVer
way (`0.1.0-rc.1`, `0.1.0-dev.2`), and so does the `.vsix` file name.
`check_versions.py` knows the mapping. A pre-release is marked as one on
GitHub, goes to TestPyPI, and has its `.vsix` on the GitHub Release; it is
never published to the VS Marketplace or Open VSX, whose versions can't be
republished.

## Release

```bash
gh workflow run prepare-release.yml -f version=X.Y.Z   # or e.g. 0.1.0rc1
gh pr list --state open          # the bot's "Release X.Y.Z" PR
```

The bot's PR only moves `VERSION` and prepends a commit list to
`CHANGELOG.md`. Before merging, on its `release/vX.Y.Z` branch:

1. Run `python scripts/check_versions.py --write`. It sets every other
   version site from `VERSION`, in the form each one takes, and checks
   them: `extension/package.json`, `ui/package.json`, the root entry of
   both `package-lock.json` files (two lines each),
   `.claude-plugin/marketplace.json` and
   `integrations/claude-code-plugin/.claude-plugin/plugin.json`.
2. In `CHANGELOG.md`, turn the curated `## Unreleased` notes into the
   `## vX.Y.Z (date)` section and drop the bot's commit list, so there is one
   heading per release.

Push, wait for Tests to pass on the PR, and merge it. Then watch:

```bash
gh run list --workflow tag-release.yml --limit 1
gh run list --workflow publish-release-packages.yml --limit 1
gh run list --workflow publish-vsix.yml --limit 1
```

A tag that fails its publish gates is burned — tags cannot be moved or
deleted, so the fix ships under a new version. TestPyPI can take a few
minutes to serve a new upload; retry an install before calling it missing.

## Secrets (repository or org)

| Secret | Purpose |
|--------|---------|
| `TESTPYPI_API_TOKEN` | Publish `pydevices-mpftp` to TestPyPI (org secret, inherited via `secrets: inherit`) |
| `VSCE_PAT` | Optional — publish the extension to VS Marketplace (`vsce publish`) |
| `OVSX_PAT` | Optional — publish the extension to Open VSX (`ovsx publish`) |

No Trusted Publisher / OIDC (`id-token: write`) is configured — mpftp's org
has not been accepted to TestPyPI's Trusted Publisher program, so publishing
uses `TESTPYPI_API_TOKEN` with `user: __token__`, exactly like every other
PyDevices repository. Without `VSCE_PAT`/`OVSX_PAT`, `publish-vsix.yml` still
attaches the `.vsix` to the GitHub Release for "Install from VSIX…" — those
two secrets only gate the marketplace publish steps.

## Local package (no publish)

```bash
python -m build .                    # sdist + wheel → dist/
./scripts/package-vsix.sh            # or: npm run package (from extension/)
python scripts/check_versions.py     # every version site agrees with VERSION
```
