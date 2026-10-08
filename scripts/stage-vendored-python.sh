#!/usr/bin/env bash
# Stage the mpftp Python package, and the pure-Python packages it needs, into
# the extension so vsce can package them.
#
# vsce only packages the directory holding package.json, so the package cannot
# be referenced across the repo — it has to be copied in. The VSIX copy is never
# pip-installed, so the canonical VERSION is copied alongside it for
# mpftp.__version__ to read.
#
# mpremote and pyserial go to extension/python/_vendor at the exact, hashed
# versions in extension/vendor-requirements.txt, with their licences, so the
# extension runs on any Python 3 without a pip install. mpftp_launch.py puts
# _vendor on sys.path ahead of site-packages. Set PYTHON to choose the
# interpreter whose pip fetches them (default python3).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/cli/src/mpftp"
DEST="$ROOT/extension/python/mpftp"
VENDOR="$ROOT/extension/python/_vendor"
PYTHON="${PYTHON:-python3}"

rm -rf "$DEST"
mkdir -p "$DEST"
rsync -a --exclude '__pycache__' --exclude '*.py[cod]' "$SRC"/ "$DEST"/
cp "$ROOT/VERSION" "$DEST/VERSION"

echo "Staged $(cd "$DEST" && ls *.py | wc -l) modules + VERSION → extension/python/mpftp"

rm -rf "$VENDOR"
"$PYTHON" -m pip install --quiet --disable-pip-version-check --no-compile \
    --no-deps --only-binary :all: --require-hashes \
    -r "$ROOT/extension/vendor-requirements.txt" --target "$VENDOR"
rm -rf "$VENDOR/bin"
find "$VENDOR" -name '__pycache__' -type d -prune -exec rm -rf {} +
mkdir -p "$VENDOR/LICENSES"
cp "$ROOT"/extension/vendor-licenses/* "$VENDOR/LICENSES/"

echo "Vendored $(cd "$VENDOR" && ls -d *.dist-info | sed 's/\.dist-info$//' | tr '\n' ' ')→ extension/python/_vendor"
