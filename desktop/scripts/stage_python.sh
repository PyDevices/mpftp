#!/usr/bin/env bash
# Stage a private Windows Python with mpftp installed into desktop/python-win/,
# which the Tauri bundle ships as resources/python/. Runs on Linux: the
# embeddable zip comes from python.org and uv resolves win_amd64 wheels.
set -euo pipefail

PYVER="${PYVER:-3.14.8}"
here="$(cd "$(dirname "$0")/.." && pwd)"
repo="$(cd "$here/.." && pwd)"
out="$here/python-win"
cache="$here/.cache"
mkdir -p "$cache"

zip="$cache/python-$PYVER-embed-amd64.zip"
[ -f "$zip" ] || curl -fsSL -o "$zip" "https://www.python.org/ftp/python/$PYVER/python-$PYVER-embed-amd64.zip"

rm -rf "$out"
mkdir -p "$out"
(cd "$out" && unzip -q "$zip")

short="$(echo "$PYVER" | cut -d. -f1,2 | tr -d .)"
# The embeddable build ignores PYTHONPATH and site-packages unless ._pth says so.
cat > "$out/python$short._pth" <<EOF
python$short.zip
.
Lib\\site-packages
import site
EOF

# Build mpftp from this checkout, then install it and its dependencies as
# Windows wheels. A few dependencies (esptool) publish only an sdist; those
# are pure Python and build here, and the check below catches anything that
# built a Linux binary instead.
rm -rf "$cache/wheel"
uv build --wheel --out-dir "$cache/wheel" "$repo" >/dev/null
uv pip install \
  --target "$out/Lib/site-packages" \
  --python-platform x86_64-pc-windows-msvc \
  --python-version "$(echo "$PYVER" | cut -d. -f1,2)" \
  "$cache"/wheel/*.whl

if find "$out" -name '*.so' | grep -q .; then
  echo "stage_python: a Linux extension module was built:" >&2
  find "$out" -name '*.so' >&2
  exit 1
fi
find "$out" -name __pycache__ -type d -prune -exec rm -rf {} +
du -sh "$out"
