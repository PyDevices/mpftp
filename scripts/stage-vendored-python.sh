#!/usr/bin/env bash
# Stage the mpftp Python package into the extension so vsce can package it.
#
# vsce only packages the directory holding package.json, so the package cannot
# be referenced across the repo — it has to be copied in. The VSIX copy is never
# pip-installed, so the canonical VERSION is copied alongside it for
# mpftp.__version__ to read.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/cli/src/mpftp"
DEST="$ROOT/extension/python/mpftp"

rm -rf "$DEST"
mkdir -p "$DEST"
rsync -a --exclude '__pycache__' --exclude '*.py[cod]' "$SRC"/ "$DEST"/
cp "$ROOT/VERSION" "$DEST/VERSION"

echo "Staged $(cd "$DEST" && ls *.py | wc -l) modules + VERSION → extension/python/mpftp"
