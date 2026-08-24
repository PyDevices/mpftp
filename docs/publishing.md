# Publishing and releases

How one root [`VERSION`](../VERSION) becomes a published
[`pydevices-mpftp`](https://test.pypi.org/project/pydevices-mpftp/) on TestPyPI
*and* a versioned VSIX on
[GitHub Releases](https://github.com/PyDevices/mpftp/releases) (and optionally
the VS Marketplace / Open VSX) — both from the same tag, in one step.

## Pipeline

```text
mpftp (VERSION committed on main, CI green)
  gh release create vX.Y.Z --target <commit> --generate-notes
           │
           ├──▶ publish-release-packages.yml
           │      reusable-publish-release-packages.yml@publishing-v3
           │      build sdist/wheel → smoke-test install → TestPyPI
           │
           └──▶ publish-vsix.yml
                  confirm tag == VERSION → npm ci → vsce package
                  → attach *.vsix to the vX.Y.Z release
                  (optional) vsce / ovsx publish when secrets are set
```

**`VERSION` is the single source of truth**, not the tag. `extension/package.json`
is committed already equal to it — `scripts/check_versions.py` enforces this in
CI on every pull request, alongside `mpftp.__version__` and
`extension/package-lock.json`. By release time nothing computes or rewrites a
version; both workflows only confirm the tag they were triggered from agrees
with the `VERSION` on the commit they checked out.

**A published GitHub Release is the trigger, not a tag push.** A `git tag` +
`git push` on its own starts nothing: both workflows key off `release:
published`. This matters because a release created with the default
`GITHUB_TOKEN` does *not* re-fire `release` events — so releasing here uses
`gh release create` directly, the same as every other PyDevices repository
(see `dotgithub/docs/publishing-automation.md`), never the GitHub web UI's
"draft" flow left unpublished.

## Version numbers

One `X.Y.Z` covers the PyPI distribution, the `mpftp` import, the VS Code
extension, and every future plugin — there is no per-component version.
Preview the next patch:

```bash
./scripts/next_release_version.sh --verbose
```

## Release (local clone)

```bash
cd mpftp
git switch main && git pull --ff-only
version="$(./scripts/next_release_version.sh)"
printf '%s\n' "$version" > VERSION
git add VERSION
git commit -m "Release $version"
git push origin main
# Wait for tests.yml to pass on main before continuing.

release_commit="$(git rev-parse HEAD)"
gh release create "v$version" \
  --target "$release_commit" \
  --generate-notes
```

That one `gh release create` starts both `publish-release-packages.yml`
(TestPyPI) and `publish-vsix.yml` (the VSIX). Watch them:

```bash
gh run list --workflow publish-release-packages.yml --limit 3
gh run list --workflow publish-vsix.yml --limit 3
```

`scripts/publish_release_tag.sh` remains available to create (and push) the
annotated tag itself, but the tag alone still does not publish anything — the
script's own output tells you the `gh release create` command to run next.

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
python scripts/check_versions.py     # VERSION, package.json, mpftp.__version__ agree
```
