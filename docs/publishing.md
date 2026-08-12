# Publishing and releases

How this repo becomes a versioned VSIX on [GitHub Releases](https://github.com/PyDevices/mpftp/releases)
(and optionally the VS Marketplace / Open VSX).

## Pipeline

```text
mpftp (commit on main)
  ./scripts/publish_release_tag.sh --push   # next patch after highest v*
           │
           ▼
publish-vsix.yml
  sync package.json version from tag
  npm ci → vsce package → GitHub Release (.vsix)
  (optional) vsce / ovsx publish when secrets are set
```

Git tags (`vX.Y.Z`) are the source of truth. CI writes that version into
`package.json` before packaging so the VSIX matches the tag. The committed
`package.json` version is only the base used when no release tags exist yet
(currently **0.0.1**).

## Version numbers

Format: **`0.0.x`** semver until promoted. Preview:

```bash
./scripts/next_release_version.sh --verbose
./scripts/publish_release_tag.sh --dry-run
```

First release (no tags yet):

```bash
./scripts/publish_release_tag.sh 0.0.1 --push
```

Later patches:

```bash
./scripts/publish_release_tag.sh --push
```

## Secrets (repository or org)

| Secret | Purpose |
|--------|---------|
| `VSCE_PAT` | Optional — publish to VS Marketplace (`vsce publish`) |
| `OVSX_PAT` | Optional — publish to Open VSX (`ovsx publish`) |

Without those secrets, the workflow still attaches the `.vsix` to the GitHub
Release for “Install from VSIX…”.

## Release (local clone)

```bash
git push origin main
./scripts/publish_release_tag.sh --push
```

Working tree must be clean. Do not hand-bump `package.json` for a release —
the tag drives the packaged version.

## Local package (no publish)

```bash
./scripts/package-vsix.sh
# or: npm run package
```
