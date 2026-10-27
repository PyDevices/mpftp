#!/usr/bin/env bash
# Stage a private Linux Python with mpftp installed into desktop/python-linux/,
# which the Tauri bundle ships as resources/python/ (tauri.linux.conf.json).
# The app runs python/bin/python3 from there.
#
# The Python is a python-build-standalone build: relocatable, linked only
# against glibc 2.17 or newer, so it runs from wherever a .deb or AppImage
# puts it. Pinned with its checksum; move it on purpose.
set -euo pipefail

PYVER="${PYVER:-3.14.8}"
PBS_TAG="${PBS_TAG:-20261020}"
PBS_SHA256="${PBS_SHA256:-b88a4ac994ac55c35a03a3672103618d5c5d5bf342db7b2093b8d328b6516b42}"
here="$(cd "$(dirname "$0")/.." && pwd)"
repo="$(cd "$here/.." && pwd)"
out="$here/python-linux"
cache="$here/.cache"
mkdir -p "$cache"

name="cpython-$PYVER+$PBS_TAG-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
tarball="$cache/$name"
[ -f "$tarball" ] || curl -fsSL -o "$tarball" \
  "https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_TAG/${name//+/%2B}"
echo "$PBS_SHA256  $tarball" | sha256sum -c --quiet - || {
  echo "stage_python_linux: checksum mismatch for $name" >&2
  rm -f "$tarball"
  exit 1
}

rm -rf "$out" "$out.tmp"
mkdir -p "$out.tmp"
tar -xzf "$tarball" -C "$out.tmp"
mv "$out.tmp/python" "$out"
rm -rf "$out.tmp"

short="$(echo "$PYVER" | cut -d. -f1,2)"
lib="$out/lib/python$short"
# What the app never uses: headers, the IDE, Tk and its Tcl, pip, and
# libpython, which is for embedding (bin/python3.X is linked statically).
rm -rf "$out/include" "$out/share" "$out/lib/pkgconfig" "$lib/idlelib" \
  "$lib/tkinter" "$lib/turtledemo" "$lib/ensurepip" "$lib/EXTERNALLY-MANAGED" \
  "$lib/site-packages"/pip "$lib/site-packages"/pip-* \
  "$out"/lib/libpython* "$out"/lib/libtcl* "$out"/lib/libtk* \
  "$out"/lib/tcl* "$out"/lib/tk* "$out"/lib/itcl* "$out"/lib/thread* \
  "$lib"/lib-dynload/_tkinter.*

# Build mpftp from this checkout and install it, with its dependencies, into
# the staged Python itself, so compiled dependencies match its ABI.
rm -rf "$cache/wheel"
uv build --wheel --out-dir "$cache/wheel" "$repo" >/dev/null
uv pip install --python "$out/bin/python3" "$cache"/wheel/*.whl

# The app runs python3 -m <module> and never a console script, whose
# shebangs would point at this staging folder anyway. Tauri copies resources
# file by file, so bin/ keeps one real file, python3, and no symlinks.
real="$(readlink -f "$out/bin/python3")"
mv "$real" "$out/python3.tmp"
rm -rf "$out/bin"
mkdir "$out/bin"
mv "$out/python3.tmp" "$out/bin/python3"
if find "$out" -type l | grep -q .; then
  echo "stage_python_linux: symlinks left, which Tauri would not copy as links:" >&2
  find "$out" -type l >&2
  exit 1
fi

# Every library must find what it links against, or the AppImage's
# linuxdeploy stops on it.
missing="$(find "$out" -name '*.so*' -type f -exec sh -c \
  'ldd "$1" 2>/dev/null | grep -q "not found" && echo "$1"' _ {} \;)"
if [[ -n "$missing" ]]; then
  echo "stage_python_linux: libraries with missing dependencies:" >&2
  echo "$missing" >&2
  exit 1
fi

"$out/bin/python3" -I -c "import mpftp.desktop_host, mpftp.sidecar, serial, mpremote, esptool, bleak"
find "$out" -name __pycache__ -type d -prune -exec rm -rf {} +
du -sh "$out"
