#!/usr/bin/env bash
# Build a .vsix for Install from VSIX / marketplace upload.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
# shellcheck disable=SC1091
[[ -s "$NVM_DIR/nvm.sh" ]] && . "$NVM_DIR/nvm.sh"

cd "$ROOT/extension"
npm install
npm run compile
npm run package
ls -la *.vsix 2>/dev/null || ls -la "$ROOT"/extension/*.vsix
echo "Install in Cursor: Extensions → … → Install from VSIX…"
