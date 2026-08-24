#!/usr/bin/env bash
# Build and install mpftp into the Cursor WSL remote extension host.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
# shellcheck disable=SC1091
[[ -s "$NVM_DIR/nvm.sh" ]] && . "$NVM_DIR/nvm.sh"

cd "$ROOT/extension"
npm install
npm run compile

VER="$(node -p "require('./package.json').version")"
NAME="$(node -p "require('./package.json').name")"
PUB="$(node -p "require('./package.json').publisher")"
EXT_DIR="$HOME/.cursor-server/extensions/${PUB}.${NAME}-${VER}"

mkdir -p "$EXT_DIR"
rsync -a --delete \
  --exclude node_modules \
  --exclude .git \
  --exclude .venv \
  --exclude src \
  --exclude .vscode \
  --exclude '*.ts' \
  --exclude tsconfig.json \
  "$ROOT"/extension/ "$EXT_DIR"/

echo "Installed → $EXT_DIR"
echo "Reload Cursor window: Developer: Reload Window"
