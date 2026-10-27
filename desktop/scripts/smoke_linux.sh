#!/usr/bin/env bash
# Start a built Linux package the way someone would and check that it works:
# the window comes up on a virtual display, the app starts its Python host
# and the sidecar with the Python it carries, and closing the app leaves no
# Python behind. CI runs this on every build; it needs xvfb-run and, for a
# .deb, sudo to install it.
#
#   smoke_linux.sh deb      path/to/mpftp_X.Y.Z_amd64.deb
#   smoke_linux.sh appimage path/to/mpftp_X.Y.Z_amd64.AppImage
set -euo pipefail

kind="$1"
pkg="$(realpath "$2")"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

case "$kind" in
  deb)
    sudo apt-get install -y -q "$pkg"
    app=/usr/bin/mpftp
    python=/usr/lib/mpftp/python/bin/python3
    ;;
  appimage)
    chmod +x "$pkg"
    (cd "$work" && "$pkg" --appimage-extract >/dev/null)
    app="$work/squashfs-root/AppRun"
    python="$(find "$work/squashfs-root" -path '*/python/bin/python3' | head -1)"
    ;;
  *) echo "smoke_linux: kind is deb or appimage" >&2; exit 2 ;;
esac

[[ -x "$python" ]] || { echo "::error::no bundled python3 in the $kind"; exit 1; }
"$python" -I -c "import sys, mpftp.desktop_host, mpftp.sidecar; print(sys.version)"

logs="$HOME/.local/share/com.pydevices.mpftp/logs"
rm -rf "$logs"
xvfb-run -a "$app" >"$work/app.out" 2>&1 &
runner=$!

pids_of() { pgrep -f -- "-m $1" || true; }
host="" sidecar=""
for _ in $(seq 60); do
  host="$(pids_of mpftp.desktop_host)"
  sidecar="$(pids_of mpftp.sidecar)"
  [[ -n "$host" && -n "$sidecar" ]] && break
  sleep 1
done
ps -eo pid,ppid,args | grep -E 'mpftp|AppRun|python3' | grep -v grep || true

fail=0
for name in host sidecar; do
  pid="${!name}"
  if [[ -z "$pid" ]]; then
    echo "::error::the $kind did not start its $name"
    fail=1
    continue
  fi
  exe="$(readlink -f "/proc/${pid%%$'\n'*}/exe")"
  echo "$name runs $exe"
  # The bundled Python, not one from the system.
  [[ "$exe" == "$(readlink -f "$python")" ]] \
    || { echo "::error::the $name runs $exe, not the bundled $python"; fail=1; }
done

app_pid="$(pgrep -f -x "$app" || pgrep -n -f 'squashfs-root/usr/bin/mpftp|/usr/bin/mpftp' || true)"
echo "app: ${app_pid:-not found}"
[[ -n "$app_pid" ]] && kill ${app_pid}
for _ in $(seq 15); do
  [[ -z "$(pids_of mpftp.desktop_host)$(pids_of mpftp.sidecar)" ]] && break
  sleep 1
done
left="$(pids_of mpftp.desktop_host)$(pids_of mpftp.sidecar)"
if [[ -n "$left" ]]; then
  echo "::error::closing the $kind left Python running"
  ps -eo pid,ppid,args | grep -E 'mpftp' | grep -v grep || true
  pkill -f -- '-m mpftp\.' || true
  fail=1
fi
kill "$runner" 2>/dev/null || true
wait "$runner" 2>/dev/null || true

echo "--- app output"; cat "$work/app.out" || true
for f in "$logs"/*.log; do [[ -f "$f" ]] && { echo "--- $f"; cat "$f"; }; done
[[ "$kind" == deb ]] && sudo apt-get remove -y -q mpftp >/dev/null
exit "$fail"
