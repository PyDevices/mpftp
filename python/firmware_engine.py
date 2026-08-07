#!/usr/bin/env python3
"""
mpftp firmware engine — build & flash MicroPython (and variants) from the
parent workspace of a ``micropython`` checkout, with user C modules / frozen
manifest auto-discovered like a cmods-style tree, but WITHOUT depending on
build_mp.sh.

This is a stdlib-only script driven by the mpftp extension (and the mpftp CLI /
agent RPC). Each subcommand runs in its own process:

  discover     resolve MicroPython / ESP-IDF / emsdk / workspace paths
  tree         list ports -> boards -> variants
  cmods        list discovered user C modules in the workspace
  artifact     report the built firmware for a port/board/variant (Ready state)
  build        make submodules + all (streams NDJSON log lines)
  clean        make clean for the selection
  flash        flash a built artifact to a device (esp32 / rp2 / samd)
  flashers     report which ports have a known flasher
  partitions   get / set / reset an esp32 partition-table override

Long-running commands (build/flash) stream newline-delimited JSON on stdout:
  {"type":"log","line":"..."}          incremental output
  {"type":"result","ok":true, ...}     final result (always last)
Short commands print a single JSON object.

Cancellation: the parent kills this process (group); child make/flash processes
are spawned in the same group and die with it.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time


def _no_window_kwargs() -> dict:
    """Avoid flashing a blank console on Windows when spawning esptool/make."""
    if os.name != "nt":
        return {}
    # CREATE_NO_WINDOW = 0x08000000 (Python 3.7+ exposes it as an attribute).
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return {"creationflags": flag}
from pathlib import Path
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #

def emit(obj: dict) -> None:
    """Stream one NDJSON record (build/flash)."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def emit_log(line: str) -> None:
    emit({"type": "log", "line": line.rstrip("\n")})


def emit_phase(state: str, text: str = "") -> None:
    """Stream a build-phase update the panel maps onto the Build pill."""
    emit({"type": "phase", "state": state, "text": text})


def emit_result(ok: bool, **kw: Any) -> None:
    emit({"type": "result", "ok": ok, **kw})


def print_json(obj: Any) -> None:
    """Single-shot JSON result (discover/tree/...)."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    sys.stdout.flush()


# --------------------------------------------------------------------------- #
# Host detection
# --------------------------------------------------------------------------- #

def detect_host() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        try:
            v = Path("/proc/version").read_text("utf-8", "replace").lower()
            if "microsoft" in v or "wsl" in v:
                return "wsl"
        except Exception:
            pass
        return "linux"
    return "linux"


HOST = detect_host()
HOME = Path.home()
MPFTP_DIR = HOME / ".mpftp"
STATE_FILE = MPFTP_DIR / "firmware.json"
ACTIVITY_LOG = MPFTP_DIR / "activity.log"


def log_activity(kind: str, message: str = "", data: Optional[dict] = None) -> None:
    """Append an NDJSON activity record (best effort), matching activityLog.ts."""
    try:
        MPFTP_DIR.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "source": "agent",
            "kind": kind,
            "message": message,
            "data": data or {},
        }
        with ACTIVITY_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# State (last selection / device / prefs — paths live in VS Code settings)
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except Exception:
        return {}


def save_state(patch: dict) -> None:
    state = load_state()
    state.update(patch)
    try:
        MPFTP_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Path discovery
#
# MicroPython: settings hint → MP_DIR → ~/micropython → workspace candidates
# (configured firmware workspace + editor open folders). No personal layouts.
#
# Port SDK trees (ESP-IDF, emsdk, …): settings → env → <workspace>/<name>.
# Same contract for every tree; no well-known home paths for individual SDKs.
# --------------------------------------------------------------------------- #

def _is_mp_tree(p: Path) -> bool:
    return (p / "ports").is_dir() and (p / "py").is_dir()


def _workspace_roots(workspace: Optional[str]) -> list[Path]:
    """Split an os.pathsep-joined list of workspace / editor folder roots."""
    if not workspace:
        return []
    out: list[Path] = []
    seen: set[str] = set()
    for part in str(workspace).split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        key = os.path.normcase(os.path.abspath(os.path.expanduser(part)))
        if key in seen:
            continue
        seen.add(key)
        out.append(Path(part).expanduser())
    return out


def _first_existing(candidates: list[Path], ok) -> Optional[Path]:
    for c in candidates:
        try:
            if c and ok(c):
                return c.resolve()
        except Exception:
            continue
    return None


def find_micropython(
    hint: Optional[str], workspace: Optional[str] = None
) -> Optional[Path]:
    """Resolve the MicroPython tree.

    Order: explicit hint → MP_DIR → ~/micropython → each workspace root
    (firmware workspace and editor folders) as the tree itself or …/micropython.
    """
    candidates: list[Path] = []
    if hint:
        candidates.append(Path(hint).expanduser())
    env = os.environ.get("MP_DIR")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(HOME / "micropython")
    for root in _workspace_roots(workspace):
        candidates.append(root)
        candidates.append(root / "micropython")
    saved = load_state().get("micropythonPath")
    if saved:
        candidates.append(Path(str(saved)).expanduser())
    return _first_existing(candidates, _is_mp_tree)


def find_sdk_tree(
    *,
    hint: Optional[str],
    env_keys: tuple[str, ...],
    workspace: Optional[Path],
    dirname: str,
    marker_file: str,
    saved_key: Optional[str] = None,
) -> Optional[Path]:
    """Resolve a port dependency tree: hint → env → workspace/<dirname>."""

    def ok(p: Path) -> bool:
        return (p / marker_file).is_file()

    candidates: list[Path] = []
    if hint:
        candidates.append(Path(hint).expanduser())
    for key in env_keys:
        v = os.environ.get(key)
        if v:
            candidates.append(Path(v).expanduser())
    if saved_key:
        saved = load_state().get(saved_key)
        if saved:
            candidates.append(Path(str(saved)).expanduser())
    if workspace:
        candidates.append(workspace / dirname)
    return _first_existing(candidates, ok)


def find_idf(hint: Optional[str], workspace: Optional[Path]) -> Optional[Path]:
    return find_sdk_tree(
        hint=hint,
        env_keys=("IDF_DIR", "IDF_PATH"),
        workspace=workspace,
        dirname="esp-idf",
        marker_file="export.sh",
        saved_key="idfPath",
    )


def find_emsdk(hint: Optional[str], workspace: Optional[Path]) -> Optional[Path]:
    return find_sdk_tree(
        hint=hint,
        env_keys=("EMSDK_DIR", "EMSDK"),
        workspace=workspace,
        dirname="emsdk",
        marker_file="emsdk_env.sh",
        saved_key="emsdkPath",
    )


# --------------------------------------------------------------------------- #
# Build-time toolchain requirements
#
# Toolchains are resolved when Build is clicked (not at panel open). Each port
# maps to the toolchain(s) its build needs. "dir" requirements are SDK trees
# (ESP-IDF/emsdk) validated by find_idf/find_emsdk and persisted via a VS Code
# config key; "command" requirements are cross-compilers looked up on PATH (plus
# any user-located bin dirs). A missing requirement is reported back to the panel
# as a structured `needToolchain` so it can prompt the user to locate it or open
# install instructions — never a raw make failure deep in the build.
# --------------------------------------------------------------------------- #

_TC_IDF = {
    "id": "esp-idf",
    "label": "ESP-IDF",
    "kind": "dir",
    "configKey": "idfPath",
    "hint": (
        "Required tree not found. Set IDF_PATH / IDF_DIR, add an esp-idf symlink "
        "under the firmware workspace, set mpftp.idfPath, or Locate… the folder "
        "(must contain export.sh)."
    ),
    # Fallback only — prefer idf_docs_url() from ports/esp32/README.md.
    "url": "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/get-started/",
}
_TC_EMSDK = {
    "id": "emsdk",
    "label": "Emscripten SDK (emsdk)",
    "kind": "dir",
    "configKey": "emsdkPath",
    "hint": (
        "Required tree not found. Set EMSDK / EMSDK_DIR, add an emsdk symlink "
        "under the firmware workspace, set mpftp.emsdkPath, or Locate… the folder "
        "(must contain emsdk_env.sh)."
    ),
    "url": "https://emscripten.org/docs/getting_started/downloads.html",
}
_TC_MINGW = {
    "id": "mingw-w64",
    "label": "MinGW-w64 GCC (x86_64-w64-mingw32-gcc)",
    "kind": "command",
    "bin": "x86_64-w64-mingw32-gcc",
    "hint": "Install mingw-w64 (e.g. apt install gcc-mingw-w64-x86-64) or locate its bin/ folder.",
    "url": "https://www.mingw-w64.org/downloads/",
}
_TC_ARM = {
    "id": "arm-none-eabi-gcc",
    "label": "GNU Arm Embedded toolchain (arm-none-eabi-gcc)",
    "kind": "command",
    "bin": "arm-none-eabi-gcc",
    "hint": "Install the GNU Arm Embedded toolchain and put arm-none-eabi-gcc on PATH, or locate its bin/ folder.",
    "url": "https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads",
}
_TC_XTENSA_LX106 = {
    "id": "xtensa-lx106-elf-gcc",
    "label": "ESP8266 toolchain (xtensa-lx106-elf-gcc)",
    "kind": "command",
    "bin": "xtensa-lx106-elf-gcc",
    "hint": "Install the xtensa-lx106-elf toolchain and put it on PATH, or locate its bin/ folder.",
    "url": "https://docs.espressif.com/projects/esp8266-rtos-sdk/en/latest/get-started/",
}
_TC_RISCV = {
    "id": "riscv64-unknown-elf-gcc",
    "label": "RISC-V toolchain (riscv64-unknown-elf-gcc)",
    "kind": "command",
    "bin": "riscv64-unknown-elf-gcc",
    "hint": "Install a riscv64-unknown-elf toolchain and put it on PATH, or locate its bin/ folder.",
    "url": "https://github.com/riscv-collab/riscv-gnu-toolchain",
}
_TC_XC16 = {
    "id": "xc16-gcc",
    "label": "Microchip XC16 (xc16-gcc)",
    "kind": "command",
    "bin": "xc16-gcc",
    "hint": "Install Microchip XC16 and put xc16-gcc on PATH, or locate its bin/ folder.",
    "url": "https://www.microchip.com/en-us/tools-resources/develop/mplab-xc-compilers",
}
_TC_PROTOC_C = {
    "id": "protoc-c",
    "label": "protobuf-c compiler (protoc-c)",
    "kind": "command",
    "bin": "protoc-c",
    "hint": "Install protobuf-c (e.g. apt install protobuf-c-compiler) so extmod can generate its sources.",
    "url": "https://github.com/protobuf-c/protobuf-c",
}

# ESP-IDF supplies the xtensa/riscv esp32 cross-compilers, so esp32 only needs
# the IDF tree itself. minimal/bare-arm are intentionally absent (accepted hard
# fails). unix/windows-host builds use system gcc (windows additionally needs
# MinGW for a real cross-build — see do_build CROSS_COMPILE handling).
TOOLCHAIN_REQUIREMENTS: dict[str, list[dict]] = {
    "esp32": [_TC_IDF],
    "webassembly": [_TC_EMSDK],
    "windows": [_TC_MINGW],
    "esp8266": [_TC_XTENSA_LX106],
    "stm32": [_TC_ARM],
    "samd": [_TC_ARM],
    "nrf": [_TC_ARM],
    "mimxrt": [_TC_ARM],
    "rp2": [_TC_ARM],
    "alif": [_TC_ARM],
    "cc3200": [_TC_ARM],
    "renesas-ra": [_TC_ARM, _TC_PROTOC_C],
    "qemu": [_TC_ARM],
    "pic16bit": [_TC_XC16],
}


def _toolchain_bin_dirs(ns: argparse.Namespace) -> list[Path]:
    """User-located cross-toolchain bin dirs (os.pathsep-joined via --toolchain-bins)."""
    raw = getattr(ns, "toolchain_bins", None) or ""
    dirs: list[Path] = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if part:
            dirs.append(Path(part).expanduser())
    return dirs


def _requirements_for(port: str, ns: argparse.Namespace) -> list[dict]:
    if port == "qemu":
        board = (getattr(ns, "board", "") or "").lower()
        if any(tok in board for tok in ("rv32", "rv64", "riscv")):
            return [_TC_RISCV]
    return TOOLCHAIN_REQUIREMENTS.get(port, [])


def _need_toolchain(req: dict) -> dict:
    return {
        "id": req["id"],
        "label": req["label"],
        "kind": req["kind"],
        "configKey": req.get("configKey"),
        "bin": req.get("bin"),
        "hint": req["hint"],
        "url": req["url"],
    }


def idf_version(idf: Path) -> Optional[str]:
    """Best-effort ESP-IDF version string (e.g. 'v5.5.2')."""
    try:
        out = subprocess.run(
            ["git", "-C", str(idf), "describe", "--tags"],
            capture_output=True, text=True, timeout=10,
        )
        v = out.stdout.strip()
        if v:
            return v
    except Exception:
        pass
    try:
        vf = idf / "version.txt"
        if vf.is_file():
            v = vf.read_text("utf-8", "replace").strip()
            if v:
                return v
    except Exception:
        pass
    return None


_RE_IDF_RECOMMENDED = re.compile(
    r"recommended version of ESP-IDF for MicroPython is (v?\d+\.\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_RE_IDF_CLONE_BRANCH = re.compile(
    r"git clone -b (v?\d+\.\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def recommended_idf_version(port_dir: Path) -> Optional[str]:
    """Recommended ESP-IDF tag from ports/esp32/README.md (e.g. 'v5.5.2').

    Prefers the explicit "recommended version … is vX.Y.Z" line; falls back to
    the ``git clone -b`` example in the same README.
    """
    try:
        txt = (port_dir / "README.md").read_text("utf-8", "replace")
    except Exception:
        return None
    m = _RE_IDF_RECOMMENDED.search(txt)
    if not m:
        m = _RE_IDF_CLONE_BRANCH.search(txt)
    if not m:
        return None
    ver = m.group(1)
    return ver if ver.startswith("v") else f"v{ver}"


def idf_docs_url(version: Optional[str] = None) -> str:
    """Espressif Getting Started URL for a specific IDF tag (not latest/stable)."""
    if version:
        ver = version if version.startswith("v") else f"v{version}"
        # Docs are published per tag: …/en/v5.5.2/esp32/get-started/
        return (
            f"https://docs.espressif.com/projects/esp-idf/en/{ver}/esp32/get-started/"
        )
    return str(_TC_IDF["url"])


def supported_idf_minors(port_dir: Path) -> set[str]:
    """Parse supported ESP-IDF major.minor versions from the esp32 port README."""
    minors: set[str] = set()
    try:
        txt = (port_dir / "README.md").read_text("utf-8", "replace")
        for m in re.finditer(r"v(\d+)\.(\d+)(?:\.\d+)?", txt):
            minors.add(f"{m.group(1)}.{m.group(2)}")
    except Exception:
        pass
    return minors


def idf_need_toolchain(port_dir: Optional[Path] = None) -> dict:
    """needToolchain payload for a missing ESP-IDF, versioned from the MP port."""
    need = dict(_TC_IDF)
    ver = recommended_idf_version(port_dir) if port_dir else None
    need["url"] = idf_docs_url(ver)
    if ver:
        need["label"] = f"ESP-IDF {ver}"
        need["hint"] = (
            f"Required tree not found (MicroPython recommends {ver}). "
            f"Set IDF_PATH / IDF_DIR, symlink esp-idf under the firmware workspace, "
            f"set mpftp.idfPath, or Locate… a checkout with export.sh."
        )
    return need


def idf_version_mismatch(idf: Path, port_dir: Path) -> Optional[dict]:
    """If the ESP-IDF version isn't supported by this esp32 port, return a
    needToolchain describing the mismatch; otherwise None. Compared at
    major.minor granularity so patch releases within a supported line pass."""
    ver = idf_version(idf)
    supported = supported_idf_minors(port_dir)
    if not ver or not supported:
        return None
    m = re.match(r"v?(\d+)\.(\d+)", ver)
    if not m:
        return None
    minor = f"{m.group(1)}.{m.group(2)}"
    if minor in supported:
        return None
    want = ", ".join("v" + s for s in sorted(supported))
    rec = recommended_idf_version(port_dir)
    return {
        "id": "esp-idf-version",
        "label": f"a supported ESP-IDF (found {ver})",
        "kind": "dir",
        "configKey": "idfPath",
        "bin": None,
        "hint": (
            f"This ESP-IDF is {ver}, but MicroPython's esp32 port supports {want}. "
            + (f"Recommended: {rec}. " if rec else "")
            + "Locate a supported ESP-IDF checkout, or set mpftp.idfPath / IDF_PATH."
        ),
        "url": idf_docs_url(rec),
    }


def _esp32_port_dir(
    ns: argparse.Namespace, workspace: Optional[Path]
) -> Optional[Path]:
    """Resolve ports/esp32 under the active MicroPython tree."""
    mp: Optional[Path] = None
    if getattr(ns, "mp", None):
        try:
            mp = Path(ns.mp).expanduser().resolve()
        except Exception:
            mp = Path(ns.mp).expanduser()
    if not mp or not _is_mp_tree(mp):
        ws = getattr(ns, "workspace", None)
        if workspace is not None and not ws:
            ws = str(workspace)
        mp = find_micropython(None, workspace=ws)
    if not mp:
        return None
    port_dir = mp / "ports" / "esp32"
    return port_dir if port_dir.is_dir() else None


def resolve_build_toolchains(
    port: str, ns: argparse.Namespace, workspace: Optional[Path]
) -> tuple[Optional[dict], list[Path]]:
    """Resolve every toolchain the port build needs.

    Returns (needToolchain | None, extra_path_dirs). extra_path_dirs are the
    user-located bin dirs, prepended to the build PATH so located command
    toolchains are visible to make.
    """
    extra = _toolchain_bin_dirs(ns)
    search_path = os.pathsep.join(
        [str(d) for d in extra] + [os.environ.get("PATH", "")]
    )
    for req in _requirements_for(port, ns):
        if req["kind"] == "dir":
            if req["configKey"] == "idfPath":
                found = find_idf(getattr(ns, "idf", None), workspace)
                if not found:
                    return idf_need_toolchain(_esp32_port_dir(ns, workspace)), extra
            elif req["configKey"] == "emsdkPath":
                found = find_emsdk(getattr(ns, "emsdk", None), workspace)
                if not found:
                    return _need_toolchain(req), extra
            else:
                return _need_toolchain(req), extra
        else:  # command
            if not shutil.which(req["bin"], path=search_path):
                return _need_toolchain(req), extra
    return None, extra


# --------------------------------------------------------------------------- #
# Tree / port model
# --------------------------------------------------------------------------- #

# Ports we can flash (others are build-only in the UI).
FLASHERS = {
    "esp32": "esptool",
    "rp2": "uf2/picotool",
    "samd": "uf2",
}


def port_kind(port_dir: Path) -> str:
    if (port_dir / "boards").is_dir():
        return "boards"
    if (port_dir / "variants").is_dir():
        return "variants"
    return "plain"


def _has_board(d: Path) -> bool:
    return (d / "mpconfigboard.mk").is_file() or (d / "mpconfigboard.cmake").is_file()


def list_board_variants(board_dir: Path) -> list[str]:
    out: list[str] = []
    for f in sorted(board_dir.glob("mpconfigvariant_*")):
        if f.suffix in (".mk", ".cmake"):
            name = f.name[len("mpconfigvariant_"):]
            name = name.rsplit(".", 1)[0]
            out.append(name)
    return sorted(set(out))


def list_port_variants(port_dir: Path) -> list[str]:
    out: list[str] = []
    vdir = port_dir / "variants"
    if vdir.is_dir():
        for d in sorted(vdir.iterdir()):
            if (d / "mpconfigvariant.mk").is_file():
                out.append(d.name)
    return out


def list_ports(mp: Path) -> list[str]:
    out: list[str] = []
    ports = mp / "ports"
    if not ports.is_dir():
        return out
    for p in sorted(ports.iterdir()):
        if (p / "Makefile").is_file():
            out.append(p.name)
    return out


def build_tree(mp: Path) -> list[dict]:
    tree: list[dict] = []
    for port in list_ports(mp):
        port_dir = mp / "ports" / port
        kind = port_kind(port_dir)
        node: dict = {
            "port": port,
            "kind": kind,
            "flashable": port in FLASHERS,
            "flasher": FLASHERS.get(port),
            "boards": [],
            "variants": [],
        }
        if kind == "boards":
            bdir = port_dir / "boards"
            for d in sorted(bdir.iterdir()):
                if d.is_dir() and _has_board(d):
                    node["boards"].append(
                        {"board": d.name, "variants": list_board_variants(d)}
                    )
        elif kind == "variants":
            node["variants"] = list_port_variants(port_dir)
        tree.append(node)
    return tree


# --------------------------------------------------------------------------- #
# User C-module discovery (the workspace is the micropython tree's parent — no
# directory named "cmods" is required anywhere)
# --------------------------------------------------------------------------- #

def workspace_of(mp: Path) -> Path:
    return mp.parent


def discover_cmods(workspace: Path) -> dict:
    """Find workspace modules: cmake/mk usermods and/or frozen manifest.py roots.

    A sibling directory counts if it has any of:
    ``micropython.cmake``, ``micropython.mk``, or ``manifest.py`` in its root.
    Make/cmake and manifest are independent — either alone is enough.
    """
    modules: list[dict] = []
    seen: set[str] = set()
    try:
        for child in sorted(workspace.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            has_cmake = (child / "micropython.cmake").is_file()
            has_mk = (child / "micropython.mk").is_file()
            has_manifest = (child / "manifest.py").is_file()
            if not (has_cmake or has_mk or has_manifest):
                continue
            if child.name in seen:
                continue
            seen.add(child.name)
            if has_cmake:
                kind = "cmake"
            elif has_mk:
                kind = "make"
            else:
                kind = "manifest"
            modules.append(
                {
                    "name": child.name,
                    "path": str(child),
                    "hasManifest": has_manifest,
                    "hasCmake": has_cmake,
                    "hasMk": has_mk,
                    "kind": kind,
                }
            )
    except Exception:
        pass
    aggregator = (workspace / "micropython.cmake").is_file()
    manifest = (workspace / "manifest-micropython.py").is_file()
    return {
        "workspaceDir": str(workspace),
        "modules": modules,
        "hasAggregator": aggregator,
        "hasManifest": manifest,
        "manifest": str(workspace / "manifest-micropython.py") if manifest else None,
    }


# --------------------------------------------------------------------------- #
# Frozen manifest / build dir / artifact resolution
# --------------------------------------------------------------------------- #

def resolve_upstream_frozen_manifest(
    port_dir: Path, kind: str, board: str, variant: str
) -> Optional[Path]:
    """Same file MicroPython would select for FROZEN_MANIFEST (parity w/ build_mp.sh)."""
    if kind == "variants":
        if variant and (port_dir / "variants" / variant / "manifest.py").is_file():
            return port_dir / "variants" / variant / "manifest.py"
        if (port_dir / "variants" / "manifest.py").is_file():
            return port_dir / "variants" / "manifest.py"
    elif kind == "boards":
        if board and variant and (port_dir / "boards" / board / f"manifest_{variant}.py").is_file():
            return port_dir / "boards" / board / f"manifest_{variant}.py"
        if board and (port_dir / "boards" / board / "manifest.py").is_file():
            return port_dir / "boards" / board / "manifest.py"
        if (port_dir / "boards" / "manifest.py").is_file():
            return port_dir / "boards" / "manifest.py"
    else:
        if (port_dir / "manifest.py").is_file():
            return port_dir / "manifest.py"
    return None


def build_dir(port_dir: Path, kind: str, board: str, variant: str) -> Optional[Path]:
    if kind == "boards":
        if board and variant:
            return port_dir / f"build-{board}-{variant}"
        if board:
            return port_dir / f"build-{board}"
        return None
    if kind == "variants":
        if variant:
            return port_dir / f"build-{variant}"
        return port_dir / "build"
    return port_dir / "build"


# Flashable artifacts first (esp32/rp2/samd), then the build-only host/wasm
# outputs: unix -> "micropython", windows -> "micropython.exe", webassembly ->
# "micropython.mjs" (its "micropython.wasm" companion lives beside it).
ARTIFACT_NAMES = [
    "firmware.uf2",
    "firmware.bin",
    "firmware.hex",
    "micropython.bin",
    "micropython.exe",
    "micropython.mjs",
    "micropython",
]


def find_artifact(bdir: Path) -> Optional[Path]:
    if not bdir or not bdir.is_dir():
        return None
    for name in ARTIFACT_NAMES:
        f = bdir / name
        if f.is_file():
            return f
    for pat in ("firmware.*", "*.uf2"):
        matches = sorted(bdir.glob(pat))
        if matches:
            return matches[0]
    return None


def artifact_info(mp: Path, port: str, board: str, variant: str) -> dict:
    port_dir = mp / "ports" / port
    kind = port_kind(port_dir)
    bdir = build_dir(port_dir, kind, board, variant)
    art = find_artifact(bdir) if bdir else None
    if art and art.is_file():
        st = art.stat()
        info = {
            "ready": True,
            "artifact": str(art),
            "size": st.st_size,
            "mtime": st.st_mtime,
            "buildDir": str(bdir),
        }
    else:
        info = {
            "ready": False,
            "artifact": None,
            "buildDir": str(bdir) if bdir else None,
        }
    # Surface the resolved default flash offset so the UI can pre-fill (and let
    # the user override) it. esp32 only — other ports don't use an offset.
    if port == "esp32":
        info["flashOffset"] = esp32_flash_offset(port_dir, board)
    return info


# --------------------------------------------------------------------------- #
# Partition override paths
# --------------------------------------------------------------------------- #

def partition_override_path(workspace: Path, board: str, variant: str) -> Path:
    # Sibling of the micropython tree (workspace == micropython's parent). The
    # build references this relative to ports/esp32 (../../../esp32_partitions/…),
    # so no MicroPython file is edited — only this CSV is authored.
    name = board + (f"_{variant}" if variant else "")
    return workspace / "esp32_partitions" / f"{name}.csv"


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #

def stream_process(cmd: list[str], cwd: Path, env: dict) -> int:
    """Run a process, streaming merged stdout/stderr as log lines. Returns rc."""
    emit_log(f"$ (cd {cwd}) {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        **_no_window_kwargs(),
    )
    assert proc.stdout
    for line in proc.stdout:
        emit_log(line)
    proc.wait()
    return proc.returncode or 0


def do_build(ns: argparse.Namespace) -> None:
    mp = Path(ns.mp).expanduser().resolve()
    if not _is_mp_tree(mp):
        emit_result(False, error=f"Not a MicroPython tree: {mp}")
        return
    workspace = workspace_of(mp)
    port = ns.port
    board = ns.board or ""
    variant = ns.variant or ""
    port_dir = mp / "ports" / port
    if not (port_dir / "Makefile").is_file():
        emit_result(False, error=f"Invalid port: {port}")
        return
    kind = port_kind(port_dir)

    jobs = str(ns.jobs) if ns.jobs else ""
    j_arg = ["-j", jobs] if jobs else ["-j"]

    # Environment: unset inherited overrides (parity with build_mp.sh).
    env = dict(os.environ)
    env.pop("USER_C_MODULES", None)
    env.pop("FROZEN_MANIFEST", None)
    env["PYTHONUNBUFFERED"] = "1"

    # Resolve the port's toolchain(s) now (build time), not at panel open. A
    # missing toolchain is reported as a structured needToolchain so the panel
    # can prompt to locate it or open install instructions.
    need, extra_bins = resolve_build_toolchains(port, ns, workspace)
    if need:
        emit_result(
            False,
            error=f"{need['label']} not found for the {port} build. {need['hint']}",
            needToolchain=need,
        )
        return
    if extra_bins:
        env["PATH"] = os.pathsep.join(
            [str(d) for d in extra_bins] + [env.get("PATH", "")]
        )
        emit_log(f"[mpftp] toolchain PATH += {os.pathsep.join(str(d) for d in extra_bins)}")

    make_args: list[str] = []

    discovery = discover_cmods(workspace)
    modules = discovery["modules"]
    use_user_modules = discovery["hasAggregator"] or any(
        m.get("hasCmake") or m.get("hasMk") or m["kind"] in ("cmake", "make")
        for m in modules
    )
    if use_user_modules:
        make_args.append(f"USER_C_MODULES={workspace}")
        emit_log(f"[mpftp] USER_C_MODULES={workspace}")
    if modules:
        names = ", ".join(m["name"] for m in modules)
        emit_log(f"[mpftp] workspace modules: {names}")
    elif not use_user_modules:
        emit_log("[mpftp] no workspace modules found; building vanilla")

    if discovery["hasManifest"]:
        make_args.append(f"FROZEN_MANIFEST={workspace / 'manifest-micropython.py'}")
        upstream = resolve_upstream_frozen_manifest(port_dir, kind, board, variant)
        if upstream:
            env["FROZEN_MANIFEST_UPSTREAM"] = str(upstream)
            emit_log(f"[mpftp] FROZEN_MANIFEST_UPSTREAM={upstream}")
    elif modules and any(m.get("hasManifest") for m in modules):
        emit_log(
            "[mpftp] workspace has module manifest.py files but no root "
            "manifest-micropython.py aggregator — frozen packages will not be included"
        )

    if kind == "boards":
        if board:
            make_args.append(f"BOARD={board}")
        if variant:
            make_args.append(f"BOARD_VARIANT={variant}")
    elif kind == "variants":
        if variant:
            make_args.append(f"VARIANT={variant}")

    # Windows is a cross-compile from Linux/WSL: force the MinGW-w64 toolchain so
    # make doesn't fall back to host gcc (which fails on <windows.h>). The MinGW
    # requirement was already resolved above, so the prefix is guaranteed present.
    if port == "windows":
        make_args.append("CROSS_COMPILE=x86_64-w64-mingw32-")
        emit_log("[mpftp] CROSS_COMPILE=x86_64-w64-mingw32-")

    # Partition override (esp32): patch the build-dir sdkconfig after reconfigure.
    part_override = None
    if port == "esp32":
        ovr = partition_override_path(workspace, board, variant)
        if ovr.is_file():
            part_override = ovr
            emit_log(f"[mpftp] partition override: {ovr}")

    # Assemble the shell script (esp32/webassembly need a sourced env).
    script_lines = ["set -e", "set -o pipefail"]
    if port == "esp32":
        idf = find_idf(ns.idf, workspace)
        if not idf:
            need = idf_need_toolchain(port_dir)
            emit_result(False, error=need["hint"], needToolchain=need)
            return
        emit_log(f"[mpftp] ESP-IDF: {idf}")
        mismatch = idf_version_mismatch(idf, port_dir)
        if mismatch:
            emit_result(
                False,
                error=f"{mismatch['label']}. {mismatch['hint']}",
                needToolchain=mismatch,
            )
            return
        script_lines.append(f'. "{idf}/export.sh"')
    elif port == "webassembly":
        emsdk = find_emsdk(ns.emsdk, workspace)
        if not emsdk:
            emit_result(False, error="emsdk not found. Set mpftp.emsdkPath or EMSDK.")
            return
        emit_log(f"[mpftp] emsdk: {emsdk}")
        script_lines.append(f'. "{emsdk}/emsdk_env.sh"')
        # The webassembly port appends -Werror after py.mk merges user-module
        # flags, so CFLAGS_USERMOD/-Wno-* can't neutralize it and CFLAGS_EXTRA
        # isn't consumed by this make-based port. emcc appends EMCC_CFLAGS after
        # the port's own flags, so a port-scoped -Wno-error there lands last and
        # relaxes the newer emsdk clang's warnings-as-errors (e.g. main.c
        # unused-but-set-global, LVGL unused-function) without patching upstream.
        werror_relief = "-Wno-error -Wno-unused-function -Wno-unused-but-set-variable"
        existing_emcc = env.get("EMCC_CFLAGS", "").strip()
        env["EMCC_CFLAGS"] = (existing_emcc + " " + werror_relief).strip()
        emit_log(f"[mpftp] EMCC_CFLAGS={env['EMCC_CFLAGS']}")

    q_args = " ".join(_shq(a) for a in make_args)

    # Host mpy-cross with cleared user modules (parity with build_mp.sh).
    script_lines.append(
        f'make -C "{mp}/mpy-cross" USER_C_MODULES= FROZEN_MANIFEST='
    )

    if ns.clean:
        script_lines.append(f'make {" ".join(j_arg)} clean {q_args}')

    submodules = f'make {" ".join(j_arg)} submodules {q_args}'
    make_all = f'make {" ".join(j_arg)} all {q_args}'
    bdir = build_dir(port_dir, kind, board, variant)

    if port == "esp32":
        # Prep first so the build dir + sdkconfig exist; that lets us patch a
        # partition override and — if the build overflows the app partition —
        # autosize the table and rebuild without touching any MicroPython file.
        script_lines.append(submodules)
        _run_shell(script_lines, port_dir, env)  # run prep so build dir exists
        if part_override is not None and bdir:
            emit_log("[mpftp] applying partition override to build sdkconfig")
            _patch_sdkconfig_partition(bdir, part_override)

        all_lines = ["set -e", "set -o pipefail",
                     *_env_prefix(port, ns, workspace), make_all]
        rc, out = _run_shell_cap(all_lines, port_dir, env)

        autosize = getattr(ns, "autosize", True)
        if rc != 0 and autosize and bdir:
            info = parse_partition_overflow(out)
            if info:
                override = partition_override_path(workspace, board, variant)
                emit_log(
                    f"[mpftp] autosize: image 0x{info['imageSize']:x} overflows "
                    f"'{info.get('partName', 'app')}' partition — growing table "
                    "and rebuilding once"
                )
                new_size = _autosize_regenerate_override(
                    port_dir, override, board, variant, info
                )
                if new_size:
                    emit_log(
                        f"[mpftp] autosize: {override.name} app -> 0x{new_size:x}; "
                        f"override at {override}"
                    )
                    _patch_sdkconfig_partition(bdir, override)
                    emit_phase(
                        "building",
                        f"Autosized app → {new_size // 1024} KiB — rebuilding",
                    )
                    rc, out = _run_shell_cap(all_lines, port_dir, env)
                else:
                    emit_log("[mpftp] autosize: no app partition to resize; giving up")
        _finish_build(rc, mp, port, board, variant)
        return

    script_lines.append(submodules)
    script_lines.append(make_all)
    rc = _run_shell(script_lines, port_dir, env)
    _finish_build(rc, mp, port, board, variant)


def _env_prefix(port: str, ns: argparse.Namespace, workspace: Path) -> list[str]:
    lines: list[str] = []
    if port == "esp32":
        idf = find_idf(ns.idf, workspace)
        if idf:
            lines.append(f'. "{idf}/export.sh"')
    elif port == "webassembly":
        emsdk = find_emsdk(ns.emsdk, workspace)
        if emsdk:
            lines.append(f'. "{emsdk}/emsdk_env.sh"')
    return lines


def _run_shell(script_lines: list[str], cwd: Path, env: dict) -> int:
    script = "\n".join(script_lines)
    proc = subprocess.Popen(
        ["bash", "-c", script],
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout
    for line in proc.stdout:
        emit_log(line)
    proc.wait()
    return proc.returncode or 0


def _run_shell_cap(script_lines: list[str], cwd: Path, env: dict) -> tuple[int, str]:
    """Like ``_run_shell`` but also captures the streamed output as text.

    Used for the esp32 ``make all`` pass so autosize can scan for the ESP-IDF
    ``app partition is too small`` error and grow the partition.
    """
    script = "\n".join(script_lines)
    proc = subprocess.Popen(
        ["bash", "-c", script],
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout
    captured: list[str] = []
    for line in proc.stdout:
        emit_log(line)
        captured.append(line)
    proc.wait()
    return proc.returncode or 0, "".join(captured)


def _finish_build(rc: int, mp: Path, port: str, board: str, variant: str) -> None:
    if rc != 0:
        emit_result(False, error=f"make failed (exit {rc})", returncode=rc)
        log_activity("firmware_build", f"failed {port}/{board}/{variant}", {"rc": rc})
        return
    info = artifact_info(mp, port, board, variant)
    save_state({"lastSelection": {"port": port, "board": board, "variant": variant}})
    emit_result(True, **info)
    log_activity("firmware_build", f"ok {port}/{board}/{variant}", {"artifact": info.get("artifact")})


def _patch_sdkconfig_partition(
    bdir: Path, override_csv: Path, flash_mb: Optional[int] = None
) -> None:
    """Point the esp32 build's sdkconfig at a custom partition CSV.

    The CSV lives in the ``esp32_partitions`` sibling of the micropython tree and
    is referenced *relative to the esp32 project dir* (``bdir.parent`` ==
    ``ports/esp32``), e.g. ``../../../esp32_partitions/<board>.csv`` — no absolute
    paths. ESP-IDF resolves ``CONFIG_PARTITION_TABLE_CUSTOM_FILENAME`` relative to
    PROJECT_DIR, so this points the build at the sibling CSV without editing any
    MicroPython file (only the build-dir sdkconfig is touched).

    Optionally set ``CONFIG_ESPTOOLPY_FLASHSIZE_*`` (from Detect/autoset) and
    append a companion ``.sdkconfig`` fragment saved beside the CSV.
    """
    sdk = bdir / "sdkconfig"
    try:
        lines = sdk.read_text("utf-8").splitlines() if sdk.is_file() else []
    except Exception:
        lines = []
    # Relative to ports/esp32 (the idf PROJECT_DIR); posix so cmake is happy.
    try:
        rel = Path(os.path.relpath(override_csv, bdir.parent)).as_posix()
    except Exception:
        rel = str(override_csv)
    drop = ["CONFIG_PARTITION_TABLE_CUSTOM", "CONFIG_PARTITION_TABLE_FILENAME"]
    if flash_mb:
        drop.append("CONFIG_ESPTOOLPY_FLASHSIZE")
    filtered = [ln for ln in lines if not ln.startswith(tuple(drop))]
    filtered.append("CONFIG_PARTITION_TABLE_CUSTOM=y")
    filtered.append(f'CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="{rel}"')
    filtered.append(f'CONFIG_PARTITION_TABLE_FILENAME="{rel}"')
    if flash_mb:
        filtered.append(f'CONFIG_ESPTOOLPY_FLASHSIZE="{flash_mb}MB"')
        filtered.append(f"CONFIG_ESPTOOLPY_FLASHSIZE_{flash_mb}MB=y")
    frag = override_csv.with_suffix(".sdkconfig")
    try:
        if frag.is_file():
            for ln in frag.read_text("utf-8").splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    filtered.append(ln)
    except Exception:
        pass
    try:
        sdk.write_text("\n".join(filtered) + "\n", encoding="utf-8")
    except Exception as e:
        emit_log(f"[mpftp] warning: could not patch sdkconfig: {e}")


def _shq(s: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./=:+-]+", s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


def do_clean(ns: argparse.Namespace) -> None:
    mp = Path(ns.mp).expanduser().resolve()
    workspace = workspace_of(mp)
    port = ns.port
    board = ns.board or ""
    variant = ns.variant or ""
    port_dir = mp / "ports" / port
    kind = port_kind(port_dir)
    make_args: list[str] = []
    if kind == "boards":
        if board:
            make_args.append(f"BOARD={board}")
        if variant:
            make_args.append(f"BOARD_VARIANT={variant}")
    elif kind == "variants" and variant:
        make_args.append(f"VARIANT={variant}")
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    lines = ["set -e", *_env_prefix(port, ns, workspace),
             f'make -j clean {" ".join(_shq(a) for a in make_args)}']
    rc = _run_shell(lines, port_dir, env)
    emit_result(rc == 0, returncode=rc)


# --------------------------------------------------------------------------- #
# Flash
# --------------------------------------------------------------------------- #

# Second-stage bootloader offset in flash by chip family. The merged
# firmware.bin starts at the bootloader, so this is where it is written when a
# board.json does not spell out deploy_options.flash_offset.
_BOOTLOADER_OFFSET_BY_MCU = {
    "esp32": "0x1000",
    "esp32s2": "0x1000",
    "esp32s3": "0x0",
    "esp32c2": "0x0",
    "esp32c3": "0x0",
    "esp32c5": "0x0",
    "esp32c6": "0x0",
    "esp32h2": "0x0",
    "esp32p4": "0x2000",
}


def esp32_flash_offset_for_family(family: str) -> str:
    """Bootloader offset from MCU family string (e.g. Thonny catalog ``family``)."""
    mcu = (family or "").lower().replace("-", "")
    return _BOOTLOADER_OFFSET_BY_MCU.get(mcu, "0x0")


def esp32_flash_offset(port_dir: Path, board: str, family: str = "") -> str:
    """Resolve the esp32 flash offset for ``board``.

    Prefers ``board.json``'s ``deploy_options.flash_offset`` from:
      1. local MicroPython tree (``port_dir/boards/<board>/board.json``)
      2. upstream GitHub copy of the same file (download mode / no checkout)
    When that is absent, infers from ``board.json`` ``mcu`` / catalog ``family``
    — classic/S2 at 0x1000, P4 at 0x2000, newer parts at 0x0.
    """
    if board and port_dir:
        bj = port_dir / "boards" / board / "board.json"
        try:
            data = json.loads(bj.read_text("utf-8"))
            explicit = data.get("deploy_options", {}).get("flash_offset")
            if explicit is not None and str(explicit).strip() != "":
                try:
                    from firmware_download import normalize_flash_offset

                    return normalize_flash_offset(explicit)
                except Exception:
                    return str(explicit)
            mcu = str(data.get("mcu", "")).lower()
            if mcu:
                return _BOOTLOADER_OFFSET_BY_MCU.get(mcu, "0x0")
        except Exception:
            pass
    if board:
        try:
            from firmware_download import resolve_remote_flash_offset

            # Catalog / UI port is esp32 for all Espressif families; GitHub path
            # is always ports/esp32/boards/<BOARD>/board.json.
            remote = resolve_remote_flash_offset(board, port="esp32")
            if remote:
                return remote
            # board.json without deploy_options.flash_offset — use its mcu.
            from firmware_download import fetch_board_json

            data = fetch_board_json(board, port="esp32")
            if data:
                mcu = str(data.get("mcu", "")).lower()
                if mcu:
                    return _BOOTLOADER_OFFSET_BY_MCU.get(mcu, "0x0")
        except Exception:
            pass
    if family:
        return esp32_flash_offset_for_family(family)
    if not board:
        return "0x0"
    # Guess from board name when no tree / family (e.g. ESP32_GENERIC_S3).
    name = board.upper()
    for key in ("ESP32P4", "ESP32C6", "ESP32C5", "ESP32C3", "ESP32C2", "ESP32S3", "ESP32S2", "ESP32"):
        if key in name.replace("_", ""):
            return esp32_flash_offset_for_family(key.lower())
    return "0x0"


def _wslpath_w(p: str) -> str:
    try:
        out = subprocess.run(
            ["wslpath", "-w", p], capture_output=True, text=True, timeout=5
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return p


def _esptool_cmd(ns: argparse.Namespace) -> list[str]:
    """Resolve an esptool invocation for this host."""
    if ns.esptool:
        # Explicit: could be a python interpreter or an esptool executable.
        val = ns.esptool
        if val.endswith((".exe", "python", "python3")) or "python" in Path(val).name:
            return [val, "-m", "esptool"]
        return [val]
    if HOST == "wsl":
        # COM ports need Windows esptool.
        for cand in (str(HOME / "bin" / "python.exe"), "python.exe"):
            if shutil.which(cand) or Path(cand).is_file():
                return [cand, "-m", "esptool"]
        return ["python.exe", "-m", "esptool"]
    if shutil.which("esptool"):
        return ["esptool"]
    if shutil.which("esptool.py"):
        return ["esptool.py"]
    return [sys.executable, "-m", "esptool"]


# Standard esp32 partition-table offset (CONFIG_PARTITION_TABLE_OFFSET).
_PARTITION_TABLE_OFFSET = 0x8000


def _esptool_reset_mode(value: str, default: str) -> str:
    """Normalize esptool v5 reset-mode names (hyphens; accept legacy underscores)."""
    v = (value or "").strip().replace("_", "-")
    return v or default


def _read_device_partition_table(base: list[str], nbytes: int) -> Optional[bytes]:
    """Read the on-device partition-table region, or None if it can't be read.

    The read runs through the same esptool as the flash (Windows esptool under
    WSL), writing to a temp file whose path is translated for that host.
    """
    import tempfile
    tmp = Path(tempfile.gettempdir()) / f"mpftp_pt_{os.getpid()}.bin"
    out_arg = _wslpath_w(str(tmp)) if HOST == "wsl" else str(tmp)
    cmd = base + [
        "--before",
        "default-reset",
        "--after",
        "no-reset",
        "read-flash",
        hex(_PARTITION_TABLE_OFFSET),
        hex(nbytes),
        out_arg,
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, **_no_window_kwargs()
        )
        if r.returncode != 0 or not tmp.is_file():
            return None
        return tmp.read_bytes()
    except Exception:
        return None
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass


def _esp32_layout_changed(base: list[str], artifact: Path) -> Optional[bool]:
    """Does the device's partition table differ from the one we're about to flash?

    Returns True if it changed (user must confirm a full erase — a moved
    vfs/storage offset otherwise leaves a stale filesystem), False if identical,
    or None if it couldn't be determined.
    """
    new_pt = artifact.parent / "partition_table" / "partition-table.bin"
    if not new_pt.is_file():
        return None
    try:
        want = new_pt.read_bytes()
    except Exception:
        return None
    got = _read_device_partition_table(base, len(want))
    if got is None:
        return None
    return got[: len(want)] != want


def flash_esp32(ns: argparse.Namespace, mp: Optional[Path], artifact: Path) -> None:
    port_dir = (mp / "ports" / ns.port) if mp else Path(".")
    family = getattr(ns, "family", "") or ""
    offset = (getattr(ns, "offset", "") or "").strip() or esp32_flash_offset(
        port_dir, ns.board or "", family=family
    )
    emit_log(f"[mpftp] flash offset {offset}")
    fw = str(artifact)
    if HOST == "wsl":
        fw = _wslpath_w(fw)
    cmd = _esptool_cmd(ns)
    base = cmd + ["-b", str(ns.baud or 460800), "-p", ns.device]

    erase = getattr(ns, "erase", False)
    if not erase:
        # A moved vfs/storage offset leaves a stale filesystem that boots corrupt.
        # Never auto-erase: warn and require an explicit erase + second Flash.
        changed = _esp32_layout_changed(base, artifact)
        if changed:
            emit_log(
                "[mpftp] partition layout on the device differs from this firmware"
            )
            emit_result(
                False,
                error=(
                    "Partition table on the device differs from this build. "
                    "Enable “Erase flash before writing” and click Flash again. "
                    "A full erase wipes the filesystem (vfs/storage) partition — "
                    "all board files will be lost."
                ),
                needEraseConfirm={
                    "reason": "partition_layout_changed",
                    "message": (
                        "The on-device partition table does not match this firmware. "
                        "Re-flashing with erase will apply the new table but wipe the "
                        "filesystem (vfs/storage) partition — all files on the board "
                        "will be lost."
                    ),
                },
            )
            return
        if changed is None:
            emit_log(
                "[mpftp] could not read on-device partition table; "
                "skipping layout check (enable Erase if the board misbehaves)"
            )

    if erase:
        emit_log("[mpftp] erasing flash…")
        rc = stream_process(base + ["erase-flash"], Path.cwd(), dict(os.environ))
        if rc != 0:
            emit_result(False, error=f"erase-flash failed (exit {rc})")
            return
    before = _esptool_reset_mode(getattr(ns, "before", "") or "", "default-reset")
    after = _esptool_reset_mode(getattr(ns, "after", "") or "", "hard-reset")
    full = base + ["--before", before, "--after", after, "write-flash", offset, fw]
    rc = stream_process(full, Path.cwd(), dict(os.environ))
    if rc != 0:
        emit_result(False, error=f"esptool failed (exit {rc})")
        return
    emit_result(True, device=ns.device, offset=offset, artifact=str(artifact))
    log_activity("firmware_flash", f"esp32 {ns.board} -> {ns.device}", {"offset": offset})


def _find_uf2_drive() -> Optional[str]:
    """Find a mounted UF2 bootloader drive (RPI-RP2, etc.)."""
    roots: list[Path] = []
    if HOST in ("wsl", "linux"):
        roots += [Path("/media"), Path("/mnt"), Path("/run/media")]
    if HOST == "windows":
        for letter in "DEFGHIJKLMNOP":
            roots.append(Path(f"{letter}:/"))
    checked: list[Path] = []
    for root in roots:
        try:
            if root.name.endswith(":") or str(root).endswith(":/"):
                checked.append(root)
            elif root.is_dir():
                for sub in root.iterdir():
                    if sub.is_dir():
                        # one more level (/media/<user>/<LABEL>)
                        checked.append(sub)
                        for sub2 in sub.iterdir() if sub.is_dir() else []:
                            checked.append(sub2)
        except Exception:
            continue
    for d in checked:
        try:
            if (d / "INFO_UF2.TXT").is_file():
                return str(d)
        except Exception:
            continue
    return None


def flash_uf2(ns: argparse.Namespace, artifact: Path) -> None:
    """rp2 / samd: copy .uf2 to the bootloader drive; rp2 falls back to picotool."""
    target = ns.device if ns.device and _looks_like_mount(ns.device) else _find_uf2_drive()
    if target:
        dest = Path(target) / artifact.name
        emit_log(f"[mpftp] copying {artifact.name} -> {dest}")
        try:
            shutil.copyfile(str(artifact), str(dest))
            try:
                # Flush; some FS need it before the board reboots.
                with open(dest, "rb+") as f:
                    os.fsync(f.fileno())
            except Exception:
                pass
            emit_result(True, device=target, artifact=str(artifact), method="uf2")
            log_activity("firmware_flash", f"uf2 -> {target}", {"artifact": str(artifact)})
            return
        except Exception as e:
            emit_log(f"[mpftp] UF2 copy failed: {e}")

    if ns.port == "rp2" and shutil.which("picotool"):
        emit_log("[mpftp] no UF2 drive; trying picotool load")
        rc = stream_process(
            ["picotool", "load", "-f", "-x", str(artifact)], Path.cwd(), dict(os.environ)
        )
        emit_result(rc == 0, method="picotool", artifact=str(artifact),
                    error=None if rc == 0 else f"picotool failed (exit {rc})")
        return

    emit_result(
        False,
        error="No UF2 bootloader drive found. Put the board in BOOTSEL/bootloader "
        "mode (double-tap reset) and select its drive, or install picotool (rp2).",
    )


def _looks_like_mount(dev: str) -> bool:
    return "/" in dev or dev.endswith(":") or dev.endswith(":/")


def do_flash(ns: argparse.Namespace) -> None:
    port = ns.port
    board = ns.board or ""
    variant = ns.variant or ""
    if port not in FLASHERS:
        emit_result(False, error=f"Flashing not supported for port '{port}'.")
        return
    mp: Optional[Path] = None
    if getattr(ns, "mp", None):
        mp = Path(ns.mp).expanduser().resolve()
    if ns.artifact:
        artifact = Path(ns.artifact).expanduser()
    else:
        if not mp:
            emit_result(False, error="No artifact path and no MicroPython tree for last build.")
            return
        info = artifact_info(mp, port, board, variant)
        if not info["ready"]:
            emit_result(False, error="No build found for this selection. Build first.")
            return
        artifact = Path(info["artifact"])
    if not artifact.is_file():
        emit_result(False, error=f"Artifact not found: {artifact}")
        return

    if port == "esp32":
        if not ns.device:
            emit_result(False, error="No device selected.")
            return
        flash_esp32(ns, mp, artifact)
    elif port in ("rp2", "samd"):
        flash_uf2(ns, artifact)
    else:
        emit_result(False, error=f"No flasher for '{port}'.")
    save_state({"lastDevice": ns.device or ""})


# --------------------------------------------------------------------------- #
# Partitions (esp32)
# --------------------------------------------------------------------------- #

def _sdkconfig_partition_csv(port_dir: Path, board: str, variant: str) -> Optional[Path]:
    """Best-effort resolve the stock partition CSV filename from sdkconfig files."""
    board_dir = port_dir / "boards" / board
    candidates: list[Path] = []
    bdir = build_dir(port_dir, "boards", board, variant)
    if bdir:
        candidates.append(bdir / "sdkconfig")
    for name in ("sdkconfig.board", "sdkconfig.defaults"):
        candidates.append(board_dir / name)
    candidates.append(port_dir / "boards" / "sdkconfig.base")
    for c in candidates:
        try:
            if not c.is_file():
                continue
            for ln in c.read_text("utf-8").splitlines():
                m = re.match(r'\s*CONFIG_PARTITION_TABLE_CUSTOM_FILENAME\s*=\s*"(.+)"', ln)
                if m:
                    csv = m.group(1)
                    p = (port_dir / csv)
                    if p.is_file():
                        return p
                    p2 = (board_dir / csv)
                    if p2.is_file():
                        return p2
        except Exception:
            continue
    # Fallback: any partitions*.csv in the board dir, else the port default.
    for p in sorted(board_dir.glob("partitions*.csv")):
        return p
    default = port_dir / "partitions-4MiBplus.csv"
    return default if default.is_file() else None


def parse_partitions_csv(text: str) -> list[dict]:
    rows: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [c.strip() for c in line.split(",")]
        while len(parts) < 6:
            parts.append("")
        rows.append(
            {
                "name": parts[0],
                "type": parts[1],
                "subtype": parts[2],
                "offset": parts[3],
                "size": parts[4],
                "flags": parts[5],
            }
        )
    return rows


def rows_to_csv(rows: list[dict]) -> str:
    header = "# Name, Type, SubType, Offset, Size, Flags\n"
    out = [header]
    for r in rows:
        out.append(
            ", ".join(
                [
                    str(r.get("name", "")),
                    str(r.get("type", "")),
                    str(r.get("subtype", "")),
                    str(r.get("offset", "")),
                    str(r.get("size", "")),
                    str(r.get("flags", "")),
                ]
            ).rstrip(", ")
        )
    return "\n".join(out) + "\n"


def _parse_size(v: str) -> Optional[int]:
    v = (v or "").strip()
    if not v:
        return None
    try:
        if v.lower().endswith("k"):
            return int(v[:-1], 0) * 1024
        if v.lower().endswith("m"):
            return int(v[:-1], 0) * 1024 * 1024
        return int(v, 0)
    except Exception:
        return None


def _table_target_size(rows: list[dict]) -> int:
    """Total flash consumed by a partition table (max offset+size)."""
    end = 0
    cursor = 0
    for r in rows:
        off = _parse_size(r.get("offset", ""))
        size = _parse_size(r.get("size", "")) or 0
        start = off if off is not None else cursor
        end = max(end, start + size)
        cursor = start + size
    return end


# Data partitions that hold the user filesystem ("storage").
_STORAGE_SUBTYPES = {"fat", "spiffs", "littlefs"}
_STORAGE_NAMES = {"vfs", "storage", "ffat", "user"}


def _is_storage_row(r: dict) -> bool:
    return r.get("type") == "data" and (
        r.get("subtype", "") in _STORAGE_SUBTYPES
        or r.get("name", "").lower() in _STORAGE_NAMES
    )


def _fmt_hex(n: int) -> str:
    return hex(int(n))


def _reflow_offsets(rows: list[dict], from_idx: int) -> None:
    """Recompute contiguous offsets for rows at/after ``from_idx`` (keep sizes)."""
    if from_idx <= 0 or from_idx > len(rows):
        return
    prev = rows[from_idx - 1]
    prev_off = _parse_size(prev.get("offset", ""))
    prev_size = _parse_size(prev.get("size", "")) or 0
    cursor = (prev_off or 0) + prev_size
    for r in rows[from_idx:]:
        size = _parse_size(r.get("size", "")) or 0
        r["offset"] = _fmt_hex(cursor)
        cursor += size


def compute_split(
    rows: list[dict], storage_bytes: int, flash_bytes: Optional[int] = None
) -> tuple[list[dict], list[str]]:
    """Resize (or create) the storage partition to ``storage_bytes``.

    Grows/shrinks an existing vfs/fat/littlefs partition; if the table has only
    firmware (e.g. P4 factory-only), appends a trailing storage partition.
    Returns (rows, warnings). Sizes are aligned down to 4 KB.
    """
    rows = [dict(r) for r in rows]
    warnings: list[str] = []
    storage_bytes = max(0, int(storage_bytes) & ~0xFFF)
    idx = next((i for i, r in enumerate(rows) if _is_storage_row(r)), None)

    if idx is None:
        end = _table_target_size(rows)
        start = (end + 0xFFFF) & ~0xFFFF  # 64 KB align
        if storage_bytes <= 0 and flash_bytes:
            storage_bytes = (flash_bytes - start) & ~0xFFF
        rows.append(
            {
                "name": "vfs",
                "type": "data",
                "subtype": "fat",
                "offset": _fmt_hex(start),
                "size": _fmt_hex(storage_bytes),
                "flags": "",
            }
        )
        idx = len(rows) - 1
        warnings.append("No storage partition in the stock table; added a trailing vfs.")
    else:
        rows[idx]["size"] = _fmt_hex(storage_bytes)

    _reflow_offsets(rows, idx)

    if flash_bytes:
        total = _table_target_size(rows)
        if total > flash_bytes:
            warnings.append(
                f"Table end 0x{total:x} exceeds flash 0x{flash_bytes:x}; reduce storage."
            )
    return rows, warnings


def validate_partitions(rows: list[dict]) -> list[str]:
    warnings: list[str] = []
    prev_end: Optional[int] = None
    for i, r in enumerate(rows):
        off = _parse_size(r.get("offset", ""))
        size = _parse_size(r.get("size", ""))
        name = r.get("name", f"row{i}")
        if size is None:
            warnings.append(f"{name}: missing/invalid size")
            continue
        if off is not None:
            if prev_end is not None and off < prev_end:
                warnings.append(f"{name}: offset 0x{off:x} overlaps previous end 0x{prev_end:x}")
            if r.get("type") == "app" and off % 0x10000 != 0:
                warnings.append(f"{name}: app offset 0x{off:x} not 64K-aligned")
            prev_end = off + size
        elif prev_end is not None:
            prev_end = prev_end + size
    return warnings


# --------------------------------------------------------------------------- #
# Autosize (esp32): grow the app partition to fit an overflowing build
# --------------------------------------------------------------------------- #

# ESP-IDF check_sizes.py failure, e.g.:
#   Error: app partition is too small for binary micropython.bin size 0x2a4e60:
#     - Part 'factory' 0/0 @ 0x10000 size 0x1f0000 (overflow 0xb4e60)
_OVERFLOW_IMG_RE = re.compile(
    r"app partition is too small for binary \S+ size (0x[0-9a-fA-F]+)"
)
_OVERFLOW_PART_RE = re.compile(
    r"Part '([^']+)'.*?@\s*(0x[0-9a-fA-F]+)\s+size\s+(0x[0-9a-fA-F]+)"
    r"\s+\(overflow\s+(0x[0-9a-fA-F]+)\)"
)

_APP_ALIGN = 0x10000   # app partitions must start on a 64 KiB boundary
_APP_HEADROOM = 0x40000  # 256 KiB slack so a slightly larger next build still fits


def parse_partition_overflow(text: str) -> Optional[dict]:
    """Parse an ESP-IDF app-partition-too-small error, or None if not present."""
    img = _OVERFLOW_IMG_RE.search(text or "")
    if not img:
        return None
    out: dict = {"imageSize": int(img.group(1), 16)}
    part = _OVERFLOW_PART_RE.search(text)
    if part:
        out.update(
            partName=part.group(1),
            partOffset=int(part.group(2), 16),
            partSize=int(part.group(3), 16),
            overflow=int(part.group(4), 16),
        )
    return out


def autosize_app_partition_size(image_size: int) -> int:
    """Smallest 64 KiB-aligned app size that fits ``image_size`` plus headroom."""
    required = (image_size + _APP_ALIGN - 1) & ~(_APP_ALIGN - 1)
    return (required + _APP_HEADROOM + _APP_ALIGN - 1) & ~(_APP_ALIGN - 1)


def resize_app_partition(rows: list[dict], part_name: str, new_size: int) -> Optional[list[dict]]:
    """Grow the named (or first) app partition to ``new_size`` and reflow the rest.

    Returns the new rows, or None if no app partition is present.
    """
    rows = [dict(r) for r in rows]
    idx = next((i for i, r in enumerate(rows) if r.get("name") == part_name), None)
    if idx is None:
        idx = next((i for i, r in enumerate(rows) if r.get("type") == "app"), None)
    if idx is None:
        return None
    rows[idx]["size"] = _fmt_hex(new_size)
    _reflow_offsets(rows, idx + 1)  # keep app offset; push later partitions up
    return rows


def _autosize_regenerate_override(
    port_dir: Path,
    override: Path,
    board: str,
    variant: str,
    info: dict,
) -> Optional[int]:
    """Write a grown partition CSV to the sibling override path.

    Base table is the current override (if any) else the stock CSV. Returns the
    new app size on success, or None if the table couldn't be resized.
    """
    if override.is_file():
        base_text = override.read_text("utf-8")
    else:
        stock = _sdkconfig_partition_csv(port_dir, board, variant)
        if not stock:
            return None
        base_text = stock.read_text("utf-8")
    rows = parse_partitions_csv(base_text)
    new_size = autosize_app_partition_size(int(info["imageSize"]))
    resized = resize_app_partition(rows, info.get("partName", "factory"), new_size)
    if resized is None:
        return None
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(rows_to_csv(resized), encoding="utf-8")
    return new_size


def do_partitions(ns: argparse.Namespace) -> None:
    mp = Path(ns.mp).expanduser().resolve()
    workspace = workspace_of(mp)
    port_dir = mp / "ports" / "esp32"
    board = ns.board or ""
    variant = ns.variant or ""
    override = partition_override_path(workspace, board, variant)

    if ns.action == "get":
        using_override = override.is_file()
        if using_override:
            text = override.read_text("utf-8")
            source = str(override)
        else:
            stock = _sdkconfig_partition_csv(port_dir, board, variant)
            if not stock:
                print_json({"error": "No partition CSV found for this board."})
                return
            text = stock.read_text("utf-8")
            source = str(stock)
        rows = parse_partitions_csv(text)
        print_json(
            {
                "rows": rows,
                "source": source,
                "usingOverride": using_override,
                "overridePath": str(override),
                "warnings": validate_partitions(rows),
            }
        )
        return

    if ns.action == "set":
        if ns.csv_file:
            text = Path(ns.csv_file).read_text("utf-8")
        elif ns.rows:
            rows = json.loads(ns.rows)
            text = rows_to_csv(rows)
        else:
            print_json({"error": "set requires --rows or --csv-file"})
            return
        rows = parse_partitions_csv(text)
        warnings = validate_partitions(rows)
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        log_activity("firmware_partitions", f"set {board}/{variant}", {"path": str(override)})
        print_json({"ok": True, "overridePath": str(override), "warnings": warnings})
        return

    if ns.action == "reset":
        if override.is_file():
            try:
                override.unlink()
            except Exception as e:
                print_json({"error": str(e)})
                return
        print_json({"ok": True, "reset": True})
        return

    if ns.action == "split":
        # Base table: current override if present, else stock.
        if override.is_file():
            base_text = override.read_text("utf-8")
        else:
            stock = _sdkconfig_partition_csv(port_dir, board, variant)
            if not stock:
                print_json({"error": "No partition CSV found for this board."})
                return
            base_text = stock.read_text("utf-8")
        base_rows = parse_partitions_csv(base_text)
        storage_bytes = int(ns.storage_bytes or 0)
        flash_bytes = int(ns.flash_bytes) if ns.flash_bytes else None
        rows, warnings = compute_split(base_rows, storage_bytes, flash_bytes)
        warnings += validate_partitions(rows)
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text(rows_to_csv(rows), encoding="utf-8")
        # Companion sdkconfig fragment carries the flash size for the build.
        if ns.flash_mb:
            frag = override.with_suffix(".sdkconfig")
            frag.write_text(
                f'CONFIG_ESPTOOLPY_FLASHSIZE="{ns.flash_mb}MB"\n'
                f"CONFIG_ESPTOOLPY_FLASHSIZE_{ns.flash_mb}MB=y\n",
                encoding="utf-8",
            )
        log_activity("firmware_partitions", f"split {board}/{variant}", {"path": str(override)})
        print_json(
            {
                "ok": True,
                "rows": rows,
                "overridePath": str(override),
                "warnings": warnings,
            }
        )
        return

    if ns.action == "candidates":
        stock = _sdkconfig_partition_csv(port_dir, board, variant)
        board_dir = port_dir / "boards" / board
        candidates: list[dict] = []
        if override.is_file():
            try:
                rows = parse_partitions_csv(override.read_text("utf-8"))
                candidates.append(
                    {
                        "label": "Current override",
                        "path": str(override),
                        "rows": rows,
                        "targetSize": _table_target_size(rows),
                        "isOverride": True,
                        "isStock": False,
                    }
                )
            except Exception:
                pass
        seen: set[str] = set()
        globbed: list[Path] = []
        if stock:
            globbed.append(stock)
        globbed += sorted(board_dir.glob("partitions*.csv"))
        globbed += sorted(port_dir.glob("partitions*.csv"))
        for p in globbed:
            rp = str(p)
            if rp in seen:
                continue
            seen.add(rp)
            try:
                rows = parse_partitions_csv(p.read_text("utf-8"))
            except Exception:
                continue
            candidates.append(
                {
                    "label": p.name,
                    "path": rp,
                    "rows": rows,
                    "targetSize": _table_target_size(rows),
                    "isOverride": False,
                    "isStock": bool(stock and p == stock),
                }
            )
        print_json(
            {
                "candidates": candidates,
                "overridePath": str(override),
                "usingOverride": override.is_file(),
            }
        )
        return


# --------------------------------------------------------------------------- #
# Detect (esptool-first chip / flash / security probe)
# --------------------------------------------------------------------------- #

# Generic MicroPython board name per ESP32 family code.
ESP_FAMILIES = {
    "S2": "ESP32_GENERIC_S2",
    "S3": "ESP32_GENERIC_S3",
    "C2": "ESP32_GENERIC_C2",
    "C3": "ESP32_GENERIC_C3",
    "C5": "ESP32_GENERIC_C5",
    "C6": "ESP32_GENERIC_C6",
    "H2": "ESP32_GENERIC_H2",
    "P4": "ESP32_GENERIC_P4",
}

# Static per-family facts esptool does not report (SRAM etc.). "" == classic ESP32.
CHIP_SPECS = {
    "":   {"label": "ESP32",    "sramKb": 520, "cores": 2, "lp": False, "maxMhz": 240},
    "S2": {"label": "ESP32-S2", "sramKb": 320, "cores": 1, "lp": False, "maxMhz": 240},
    "S3": {"label": "ESP32-S3", "sramKb": 512, "cores": 2, "lp": False, "maxMhz": 240},
    "C2": {"label": "ESP32-C2", "sramKb": 272, "cores": 1, "lp": False, "maxMhz": 120},
    "C3": {"label": "ESP32-C3", "sramKb": 400, "cores": 1, "lp": False, "maxMhz": 160},
    "C5": {"label": "ESP32-C5", "sramKb": 384, "cores": 1, "lp": True,  "maxMhz": 240},
    "C6": {"label": "ESP32-C6", "sramKb": 512, "cores": 1, "lp": True,  "maxMhz": 160},
    "H2": {"label": "ESP32-H2", "sramKb": 320, "cores": 1, "lp": False, "maxMhz": 96},
    "P4": {"label": "ESP32-P4", "sramKb": 768, "cores": 2, "lp": True,  "maxMhz": 400},
}


def family_from_chip(chip: str) -> str:
    """Family code (S3/C6/P4/...) from an esptool chip name; '' for classic ESP32."""
    m = re.search(r"ESP32-?(S2|S3|C2|C3|C5|C6|H2|P4)\b", (chip or "").upper())
    return m.group(1) if m else ""


def family_from_text(text: str) -> str:
    """Family code from a MicroPython machine/uname string; '' for classic ESP32."""
    m = re.search(r"ESP32-?(S2|S3|C2|C3|C5|C6|H2|P4)", (text or "").upper())
    return m.group(1) if m else ""


def parse_esptool_flash_id(text: str) -> dict:
    """Parse ``esptool flash_id`` output (works with or without MicroPython)."""
    out: dict = {
        "chip": "",
        "revision": "",
        "features": [],
        "cores": None,
        "lpCore": False,
        "maxMhz": None,
        "crystalMhz": None,
        "mac": "",
        "flashMb": None,
        "psram": {"present": False, "octal": False, "label": "", "sizeMb": None},
        "usbMode": "",
    }
    # esptool v5: "Chip type:  ESP32-P4 (revision v1.3)";
    # esptool v4: "Chip is ESP32-P4 (revision v1.3)".
    m = re.search(
        r"Chip (?:is|type:)\s+(.+?)(?: \(QFN\d+\))? \(revision (v?[^)]+)\)", text
    )
    if m:
        out["chip"] = m.group(1).strip()
        out["revision"] = m.group(2).strip()
    else:
        m2 = re.search(
            r"(?:Chip (?:is|type:)\s+|Connected to\s+|Detecting chip type\W+)(ESP32\S*)",
            text,
        )
        if m2:
            out["chip"] = m2.group(1).strip()
    fm = re.search(r"Features:\s*(.+)", text)
    if fm:
        feats = [f.strip() for f in fm.group(1).split(",") if f.strip()]
        out["features"] = feats
        for f in feats:
            low = f.lower()
            if "single core" in low:
                out["cores"] = 1
            elif "dual core" in low:
                out["cores"] = 2
            if "lp core" in low:
                out["lpCore"] = True
            mh = re.search(r"(\d+)\s*MHz", f)
            if mh and out["maxMhz"] is None:
                out["maxMhz"] = int(mh.group(1))
            if "psram" in low:
                out["psram"]["present"] = True
                out["psram"]["label"] = f
                if "octal" in low:
                    out["psram"]["octal"] = True
                # Embedded-PSRAM chips report size in the feature line, e.g.
                # "Embedded PSRAM 2MB". External (S3/P4) PSRAM usually omits it —
                # the MicroPython runtime probe fills that in when available.
                ps = re.search(r"(\d+)\s*MB", f)
                if ps:
                    out["psram"]["sizeMb"] = int(ps.group(1))
    # v4: "Crystal is 40 MHz"; v5: "Crystal frequency:  40MHz".
    cm = re.search(r"Crystal (?:is|frequency:)\s*(\d+)\s*MHz", text)
    if cm:
        out["crystalMhz"] = int(cm.group(1))
    mac = re.search(r"MAC:\s*([0-9a-fA-F:]{17})", text)
    if mac:
        out["mac"] = mac.group(1).lower()
    fl = re.search(r"[Ff]lash size:\s*(\d+)\s*MB", text)
    if fl:
        out["flashMb"] = int(fl.group(1))
    if "USB-Serial/JTAG" in text:
        out["usbMode"] = "USB-Serial/JTAG"
    elif "USB-OTG" in text:
        out["usbMode"] = "USB-OTG"
    return out


def parse_esptool_security(text: str) -> dict:
    """Parse ``esptool get_security_info``; tolerate 'not implemented' chips."""
    out = {"available": False, "secureBoot": "", "flashEncryption": ""}
    sb = re.search(r"Secure Boot:\s*(\w+)", text)
    fe = re.search(r"Flash Encryption:\s*(\w+)", text)
    if sb:
        out["secureBoot"] = sb.group(1)
        out["available"] = True
    if fe:
        out["flashEncryption"] = fe.group(1)
        out["available"] = True
    return out


def esp32_board_variants(tree: Optional[list], board: str) -> Optional[list]:
    """Variants for an esp32 board in the tree, or None if the board is absent."""
    for node in tree or []:
        if node.get("port") == "esp32":
            for b in node.get("boards", []):
                if b.get("board") == board:
                    return b.get("variants", [])
    return None


def match_esp_target(
    family: str, psram: dict, flash_mb: Optional[int], mp_hints: dict, tree: Optional[list]
) -> dict:
    """Suggest board / variant / flash-size for an ESP32 family (fixture rules)."""
    notes: list[str] = []
    generic = ESP_FAMILIES.get(family, "ESP32_GENERIC")
    variants = esp32_board_variants(tree, generic)
    matched = variants is not None
    avail = variants or []

    def has(v: str) -> bool:
        return v in avail

    build = str(mp_hints.get("build") or "").upper()
    machine = str(mp_hints.get("machine") or "")
    memfree = mp_hints.get("memfree") or 0
    variant = ""
    variant_options: list[str] = []

    if family == "P4":
        # External Wi-Fi co-processor (C5/C6) is invisible to esptool: MP only.
        # Trust MicroPython hints even when the tree/catalog has not listed
        # variants yet (download mode scrapes C6_WIFI after Detect).
        hint_blob = " ".join(
            str(mp_hints.get(k) or "")
            for k in ("build", "machine", "version", "platform")
        ).upper()
        for w in ("C6_WIFI", "C5_WIFI"):
            if w in hint_blob:
                variant = w
                break
        variant_options = [v for v in avail if v.upper().endswith("WIFI")]
        if variant and variant not in variant_options:
            variant_options = [variant] + variant_options
        if not variant and variant_options:
            notes.append(
                "P4 external Wi-Fi (C5/C6) cannot be detected from esptool alone — "
                "pick the variant if your board has an external radio."
            )
    else:
        if psram.get("octal") or "octal-spiram" in machine.lower():
            variant = "SPIRAM_OCT" if has("SPIRAM_OCT") else ("SPIRAM" if has("SPIRAM") else "")
        elif psram.get("present"):
            variant = "SPIRAM" if has("SPIRAM") else ("SPIRAM_OCT" if has("SPIRAM_OCT") else "")
        elif isinstance(memfree, (int, float)) and memfree > 1_000_000:
            if has("SPIRAM"):
                variant = "SPIRAM"
                notes.append("Large MicroPython heap suggests PSRAM; selected SPIRAM.")
        variant_options = [v for v in avail if "SPIRAM" in v]

    confidence = "matched"
    if not matched:
        confidence = "family-only"
        notes.append(
            f"{generic} not found in this MicroPython tree; using the family default."
        )

    return {
        "port": "esp32",
        "board": generic,
        "variant": variant,
        "variantOptions": variant_options,
        "flashSize": f"{flash_mb}MB" if flash_mb else "",
        "flashConfig": f"CONFIG_ESPTOOLPY_FLASHSIZE_{flash_mb}MB" if flash_mb else "",
        "confidence": confidence,
        "notes": notes,
    }


def _mp_indicates_esp(h: dict) -> bool:
    plat = str(h.get("platform") or "").lower()
    machine = str(h.get("machine") or "").lower()
    return plat in ("esp32", "espressif") or "esp32" in machine


def suggested_port_from_mp(h: dict) -> str:
    """Best firmware port for a non-Espressif board from MicroPython/CP hints."""
    low = str(h.get("platform") or "").lower()
    machine = str(h.get("machine") or "").lower()
    mapping = {
        "rp2": "rp2",
        "rp2040": "rp2",
        "pyboard": "stm32",
        "mimxrt": "mimxrt",
        "samd": "samd",
        "nrf52": "nrf",
        "nrf52840": "nrf",
        "esp32": "esp32",
        "espressif": "esp32",
    }
    if low in mapping:
        return mapping[low]
    if "rp2" in low or "rp2040" in machine:
        return "rp2"
    if "nrf" in low or "nrf" in machine:
        return "nrf"
    if "samd" in low or "samd" in machine:
        return "samd"
    if "imxrt" in low or "imxrt" in machine:
        return "mimxrt"
    if "stm32" in machine or low == "pyboard":
        return "stm32"
    if "espressif" in low or "esp32" in machine:
        return "esp32"
    return ""


def _esptool_fail_reason(out: str, rc: int) -> str:
    for key in (
        "No serial data received",
        "Invalid head of packet",
        "Failed to connect",
        "could not open",
        "Timed out",
    ):
        if key.lower() in (out or "").lower():
            return f"esptool: {key}"
    if rc == 127:
        return "esptool not available"
    return "Not an Espressif chip (esptool did not detect an ESP)"


def _esptool_capture(ns: argparse.Namespace, sub_args: list[str], timeout: int = 60):
    """Run one esptool subcommand, returning (rc, combined_output)."""
    cmd = _esptool_cmd(ns) + ["-p", ns.device] + sub_args
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, **_no_window_kwargs()
        )
        return r.returncode, (r.stdout or "") + "\n" + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"esptool timed out after {timeout}s"
    except FileNotFoundError as e:
        return 127, f"esptool not found: {e}"
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


def do_detect(ns: argparse.Namespace) -> None:
    device = ns.device
    if not device:
        print_json({"ok": False, "error": "No device specified for detect."})
        return

    mp_hints: dict = {}
    if ns.mp_hints:
        try:
            mp_hints = json.loads(ns.mp_hints)
        except Exception:
            mp_hints = {}

    tree = None
    if ns.mp:
        mp = Path(ns.mp).expanduser().resolve()
        if _is_mp_tree(mp):
            tree = build_tree(mp)

    # Always hard-reset after probing. esptool's default download-mode entry
    # otherwise leaves the chip in "waiting for download" and Connect fails
    # until a button reset (seen on ESP32-P4 + USB-UART bridges).
    _esp_probe = ["--before", "default-reset", "--after", "hard-reset"]
    rc, fout = _esptool_capture(ns, _esp_probe + ["flash-id"])
    flash = parse_esptool_flash_id(fout)
    sec = {"available": False, "secureBoot": "", "flashEncryption": ""}
    if flash.get("chip"):
        _src, srout = _esptool_capture(ns, _esp_probe + ["get-security-info"])
        sec = parse_esptool_security(srout)

    esp_from_mp = _mp_indicates_esp(mp_hints)
    espressif = bool(flash.get("chip")) or esp_from_mp
    if not espressif:
        suggested = suggested_port_from_mp(mp_hints)
        print_json(
            {
                "ok": True,
                "espressif": False,
                "device": device,
                "reason": _esptool_fail_reason(fout, rc),
                "suggestedPort": suggested,
                "mp": mp_hints,
                "match": {
                    "port": suggested,
                    "confidence": "family-only" if suggested else "unknown",
                    "notes": [],
                },
            }
        )
        return

    if flash.get("chip"):
        family = family_from_chip(flash["chip"])
    else:
        family = family_from_text(
            str(mp_hints.get("machine") or mp_hints.get("platform") or "")
        )

    spec = CHIP_SPECS.get(family or "", CHIP_SPECS[""])
    flash_mb = flash.get("flashMb")
    if not flash_mb and mp_hints.get("flash"):
        try:
            flash_mb = int(int(mp_hints["flash"]) / (1024 * 1024))
        except Exception:
            flash_mb = None
    match = match_esp_target(family or "", flash.get("psram", {}), flash_mb, mp_hints, tree)

    result = {
        "ok": True,
        "espressif": True,
        "device": device,
        "esptoolFailed": not flash.get("chip"),
        "chip": flash.get("chip") or spec["label"],
        "revision": flash.get("revision", ""),
        "features": flash.get("features", []),
        "cores": flash.get("cores") or spec["cores"],
        "lpCore": bool(flash.get("lpCore") or spec["lp"]),
        "maxMhz": flash.get("maxMhz") or spec["maxMhz"],
        "crystalMhz": flash.get("crystalMhz"),
        "mac": flash.get("mac", ""),
        "flashMb": flash_mb,
        "psram": flash.get(
            "psram", {"present": False, "octal": False, "label": "", "sizeMb": None}
        ),
        "sramKb": spec["sramKb"],
        "security": sec,
        "match": match,
        "mp": mp_hints,
    }
    save_state(
        {
            "lastDetect": {
                "device": device,
                "chip": result["chip"],
                "secureBoot": sec.get("secureBoot", ""),
                "flashEncryption": sec.get("flashEncryption", ""),
            }
        }
    )
    log_activity(
        "firmware_detect",
        f"{result['chip']} on {device}",
        {"board": match.get("board"), "flashMb": flash_mb},
    )
    print_json(result)


# --------------------------------------------------------------------------- #
# Discover / dispatch
# --------------------------------------------------------------------------- #

def do_discover(ns: argparse.Namespace) -> None:
    # Toolchains (ESP-IDF/emsdk/cross-gcc) are resolved at build time, not here.
    # Discovery only reports the MicroPython tree, its workspace, and the host.
    ws = getattr(ns, "workspace", None)
    mp = find_micropython(ns.mp, workspace=ws)
    workspace = workspace_of(mp) if mp else None
    result = {
        "host": HOST,
        "micropython": str(mp) if mp else None,
        "workspace": str(workspace) if workspace else None,
        "state": load_state(),
    }
    if mp:
        save_state({"micropythonPath": str(mp)})
    print_json(result)


def do_tree(ns: argparse.Namespace) -> None:
    ws = getattr(ns, "workspace", None)
    mp = (
        Path(ns.mp).expanduser().resolve()
        if ns.mp
        else find_micropython(None, workspace=ws)
    )
    if not mp or not _is_mp_tree(mp):
        print_json({"error": "MicroPython tree not found", "ports": []})
        return
    print_json(
        {
            "micropython": str(mp),
            "workspace": str(workspace_of(mp)),
            "ports": build_tree(mp),
        }
    )


def do_cmods(ns: argparse.Namespace) -> None:
    ws = getattr(ns, "workspace", None)
    mp = (
        Path(ns.mp).expanduser().resolve()
        if ns.mp
        else find_micropython(None, workspace=ws)
    )
    if not mp:
        print_json({"error": "MicroPython tree not found", "modules": []})
        return
    print_json(discover_cmods(workspace_of(mp)))


def do_artifact(ns: argparse.Namespace) -> None:
    mp = Path(ns.mp).expanduser().resolve()
    print_json(artifact_info(mp, ns.port, ns.board or "", ns.variant or ""))


def do_download_tree(ns: argparse.Namespace) -> None:
    from firmware_download import catalog_tree

    force = bool(getattr(ns, "force", False))
    print_json(catalog_tree(force=force))


def do_download_list(ns: argparse.Namespace) -> None:
    from firmware_download import list_board

    board = ns.board or ""
    if not board:
        print_json({"error": "board required"})
        raise SystemExit(1)
    try:
        print_json(
            list_board(
                board,
                mp_variant=getattr(ns, "variant", None) or "",
                include_preview_probe=bool(getattr(ns, "preview", False)),
                force=bool(getattr(ns, "force", False)),
            )
        )
    except Exception as e:  # noqa: BLE001
        print_json({"error": str(e)})
        raise SystemExit(1)


def do_download(ns: argparse.Namespace) -> None:
    from firmware_download import download_file, find_variant, load_catalog, pick_download

    board = ns.board or ""
    if not board:
        emit_result(False, error="board required")
        return
    try:
        def progress(done: int, total: int) -> None:
            if total:
                pct = int(100 * done / total)
                emit_log(f"[mpftp] download {pct}% ({done}/{total})")

        force = bool(getattr(ns, "force", False))
        mp_variant = getattr(ns, "variant", None) or ""
        catalog = load_catalog(force=force)
        variant = find_variant(board, catalog=catalog)
        if not variant:
            raise RuntimeError(f"board not in download catalog: {board}")
        chosen = pick_download(
            variant,
            version=getattr(ns, "version", None) or None,
            preview=bool(getattr(ns, "preview", False)),
            mp_variant=mp_variant,
        )
        emit_log(f"[mpftp] downloading {chosen['url']}")
        path = download_file(chosen["url"], progress=progress)
        st = path.stat()
        from firmware_download import resolve_remote_flash_offset

        flash_offset = resolve_remote_flash_offset(
            variant["board"], port=variant["port"] or "esp32"
        )
        emit_result(
            True,
            ready=True,
            artifact=str(path),
            size=st.st_size,
            mtime=st.st_mtime,
            source="download",
            board=variant["board"],
            variant=mp_variant,
            version=chosen["version"],
            family=variant["family"],
            port=variant["port"],
            url=chosen["url"],
            info_url=variant["info_url"],
            vendor=variant["vendor"],
            model=variant["model"],
            flashOffset=flash_offset or None,
        )
    except Exception as e:  # noqa: BLE001
        emit_result(False, error=str(e))


def do_flashers(_ns: argparse.Namespace) -> None:
    print_json({"flashers": FLASHERS})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="firmware_engine", description="mpftp firmware engine")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_mp(sp: argparse.ArgumentParser, required: bool = False) -> None:
        sp.add_argument("--mp", default=None, required=required, help="MicroPython tree path")
        sp.add_argument(
            "--workspace",
            default=None,
            help="Editor workspace folder(s), os.pathsep-joined; used to find micropython/",
        )
        sp.add_argument("--idf", default=None, help="ESP-IDF path")
        sp.add_argument("--emsdk", default=None, help="emsdk path")
        sp.add_argument(
            "--toolchain-bins",
            default="",
            help="os.pathsep-joined cross-toolchain bin dirs prepended to the build PATH",
        )

    d = sub.add_parser("discover")
    add_mp(d)
    d.set_defaults(func=do_discover)

    t = sub.add_parser("tree")
    add_mp(t)
    t.set_defaults(func=do_tree)

    c = sub.add_parser("cmods")
    add_mp(c)
    c.set_defaults(func=do_cmods)

    a = sub.add_parser("artifact")
    add_mp(a, required=True)
    a.add_argument("--port", required=True)
    a.add_argument("--board", default="")
    a.add_argument("--variant", default="")
    a.set_defaults(func=do_artifact)

    b = sub.add_parser("build")
    add_mp(b, required=True)
    b.add_argument("--port", required=True)
    b.add_argument("--board", default="")
    b.add_argument("--variant", default="")
    b.add_argument("--clean", action="store_true")
    b.add_argument("--jobs", type=int, default=0)
    b.add_argument("--no-autosize", dest="autosize", action="store_false", default=True,
                   help="disable esp32 partition autosize-on-overflow (grow app + rebuild once)")
    b.set_defaults(func=do_build)

    cl = sub.add_parser("clean")
    add_mp(cl, required=True)
    cl.add_argument("--port", required=True)
    cl.add_argument("--board", default="")
    cl.add_argument("--variant", default="")
    cl.set_defaults(func=do_clean)

    f = sub.add_parser("flash")
    add_mp(f, required=False)  # optional when --artifact is a downloaded file
    f.add_argument("--port", required=True)
    f.add_argument("--board", default="")
    f.add_argument("--variant", default="")
    f.add_argument("--family", default="", help="MCU family for flash offset (download mode)")
    f.add_argument("--device", default="")
    f.add_argument("--artifact", default="")
    f.add_argument("--baud", type=int, default=460800)
    f.add_argument("--offset", default="",
                   help="esp32 flash offset override (default: board.json / chip family)")
    f.add_argument("--erase", action="store_true")
    f.add_argument(
        "--before",
        default="default-reset",
        type=lambda s: str(s).replace("_", "-"),
        choices=["default-reset", "no-reset", "usb-reset"],
        help="esp32 reset mode before flashing (esptool --before)",
    )
    f.add_argument(
        "--after",
        default="hard-reset",
        type=lambda s: str(s).replace("_", "-"),
        choices=["hard-reset", "soft-reset", "no-reset"],
        help="esp32 reset mode after flashing (esptool --after)",
    )
    f.add_argument("--esptool", default=None, help="esptool interpreter/executable")
    f.set_defaults(func=do_flash)

    dlt = sub.add_parser("download-tree", help="Official firmware catalog (Thonny JSON)")
    dlt.add_argument("--force", action="store_true", help="refresh catalog cache")
    dlt.set_defaults(func=do_download_tree)

    dll = sub.add_parser("download-list", help="List downloadable versions for a board")
    dll.add_argument("--board", required=True)
    dll.add_argument(
        "--variant",
        default="",
        help="MP board variant (e.g. C6_WIFI for ESP32_GENERIC_P4)",
    )
    dll.add_argument("--preview", action="store_true", help="probe board page for latest preview")
    dll.add_argument("--force", action="store_true")
    dll.set_defaults(func=do_download_list)

    dld = sub.add_parser("download", help="Download official firmware for a board")
    dld.add_argument("--board", required=True)
    dld.add_argument(
        "--variant",
        default="",
        help="MP board variant (e.g. C6_WIFI for ESP32_GENERIC_P4)",
    )
    dld.add_argument("--version", default="", help="release version (e.g. 1.28.0)")
    dld.add_argument("--preview", action="store_true", help="latest preview build")
    dld.add_argument("--force", action="store_true", help="refresh catalog cache")
    dld.set_defaults(func=do_download)

    fl = sub.add_parser("flashers")
    fl.set_defaults(func=do_flashers)

    dt = sub.add_parser("detect")
    add_mp(dt)
    dt.add_argument("--device", required=True)
    dt.add_argument("--baud", type=int, default=460800)
    dt.add_argument("--esptool", default=None, help="esptool interpreter/executable")
    dt.add_argument("--mp-hints", dest="mp_hints", default=None,
                    help="JSON of MicroPython runtime hints (optional enrichment)")
    dt.set_defaults(func=do_detect)

    pt = sub.add_parser("partitions")
    add_mp(pt, required=True)
    pt.add_argument("action", choices=["get", "set", "reset", "candidates", "split"])
    pt.add_argument("--board", default="")
    pt.add_argument("--variant", default="")
    pt.add_argument("--rows", default=None, help="JSON array of partition rows (set)")
    pt.add_argument("--csv-file", dest="csv_file", default=None, help="CSV file to import (set)")
    pt.add_argument("--storage-bytes", dest="storage_bytes", type=int, default=0,
                    help="storage partition size in bytes (split)")
    pt.add_argument("--flash-bytes", dest="flash_bytes", type=int, default=0,
                    help="total flash in bytes (split, for validation)")
    pt.add_argument("--flash-mb", dest="flash_mb", type=int, default=0,
                    help="flash size in MB for the sdkconfig fragment (split)")
    pt.set_defaults(func=do_partitions)

    return p


def main(argv: Optional[list[str]] = None) -> None:
    ns = build_parser().parse_args(argv)
    try:
        ns.func(ns)
    except BrokenPipeError:
        pass
    except Exception as e:  # noqa: BLE001
        if ns.cmd in ("build", "clean", "flash", "download"):
            emit_result(False, error=str(e))
        else:
            print_json({"error": str(e)})
        raise SystemExit(1)


if __name__ == "__main__":
    main()
