#!/usr/bin/env python3
"""
mpftp firmware engine — build MicroPython (or CircuitPython-compatible)
firmware with micropython-pydevices' ``build_mp.py``, and flash it.

A build is one build_mp.py run: mpftp finds a micropython-pydevices checkout
(see firmware_build.find_build_system), passes it the port, board, variant,
modules and flash size you chose, and reports its result. What you can choose
is what that build_mp.py offers, so ``firmware list`` and ``firmware modules``
ask it rather than reading folders themselves.

This is a stdlib-only script driven by the mpftp extension (and the mpftp CLI /
agent RPC). Each subcommand runs in its own process:

  discover     resolve the MicroPython tree, workspace and build_mp.py checkout
  tree         list build_mp.py's ports -> boards -> variants
               (the mpftp CLI calls this ``firmware list``)
  modules      list build_mp.py's modules (``cmods`` is the old name)
  artifact     report the built firmware for a port/board/variant (Ready state)
  build        run build_mp.py (streams NDJSON log lines)
  clean        delete the selection's build dir
  flash        flash a built artifact to a device (esp32 / rp2 / samd)
  flashers     report which ports have a known flasher
  partitions   get / set / reset an esp32 partition-table override

Long-running commands (build/flash) stream newline-delimited JSON on stdout:
  {"type":"log","line":"..."}          incremental output
  {"type":"result","ok":true, ...}     final result (always last)
Short commands print a single JSON object.

Cancellation: the parent kills this process (group); child build/flash processes
are spawned in the same group and die with it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from . import config, uf2
from . import firmware_build as fb


def _no_window_kwargs() -> dict:
    """Avoid flashing a blank console on Windows when spawning esptool/make."""
    if os.name != "nt":
        return {}
    # CREATE_NO_WINDOW = 0x08000000 (Python 3.7+ exposes it as an attribute).
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return {"creationflags": flag}

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
# State (last selection / device / prefs) — the "firmware" key of
# ~/.mpftp/config.json. Was its own firmware.json; folded in with the rest of
# the config, so there is exactly one settings file to reason about.
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    return config.load_firmware_state()


def save_state(patch: dict) -> None:
    config.save_firmware_state(patch)


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


def resolve_command_toolchains(
    port: str, ns: argparse.Namespace
) -> tuple[Optional[dict], list[Path]]:
    """The cross-compilers a port's build needs on PATH (arm-none-eabi-gcc for
    rp2, MinGW for windows, ...). SDK trees (ESP-IDF, emsdk) are build_mp.py's
    to fetch at their locked versions, so they aren't checked here."""
    extra = _toolchain_bin_dirs(ns)
    if port == "windows" and os.name == "nt":
        return None, extra  # build_mp.py uses MSYS2's own MinGW gcc there
    search_path = os.pathsep.join([str(d) for d in extra] + [os.environ.get("PATH", "")])
    for req in _requirements_for(port, ns):
        if req["kind"] == "command" and not shutil.which(req["bin"], path=search_path):
            return _need_toolchain(req), extra
    return None, extra


# --------------------------------------------------------------------------- #
# Tree / port model
# --------------------------------------------------------------------------- #

# How long to wait for a UF2 bootloader volume to unmount after the copy.
# Generous because the board erases and writes flash before it reboots, and a
# false timeout here reports failure on a flash that worked.
UF2_REBOOT_TIMEOUT = 30.0

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
    """A MicroPython checkout's own ports -> boards -> variants.

    Detect uses it to match a chip to upstream's boards. What a build offers
    comes from build_mp.py instead (offer_ports).
    """
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
# Builds: micropython-pydevices' build_mp.py (mpftp#75)
#
# mpftp doesn't run make for a build. It finds a micropython-pydevices
# checkout and runs its build_mp.py, and the ports, boards, variants and
# modules it lists are build_mp.py's own. The mechanics (finding the checkout,
# the listing, the command line, reading its errors) are in firmware_build.py.
# --------------------------------------------------------------------------- #

def _configured_build_system() -> str:
    try:
        return str(config.load().get("buildSystemPath") or "")
    except config.ConfigError:
        return ""


def locate_build_system(ns: argparse.Namespace) -> Optional[Path]:
    """The checkout to build with, or None. An explicit --build-system that
    isn't one raises fb.BuildSystemError."""
    return fb.find_build_system(
        getattr(ns, "build_system", None) or None,
        configured=_configured_build_system(),
        mp=getattr(ns, "mp", None) or None,
        workspace=getattr(ns, "workspace", None) or None,
    )


def _interpreter(ns: argparse.Namespace) -> str:
    return getattr(ns, "interpreter", None) or "micropython"


def offer_ports(info: dict) -> list[dict]:
    """build_mp.py's ports, each marked with whether mpftp can flash it."""
    mp = info.get("interpreter", "micropython") == "micropython"
    out = []
    for node in info.get("ports") or []:
        flasher = FLASHERS.get(node["port"]) if mp else None
        out.append(dict(node, flashable=bool(flasher), flasher=flasher))
    return out


def offer_modules(info: dict) -> list[dict]:
    """build_mp.py's modules, in the shape the Firmware panel lists."""
    return [
        dict(m, freezeOnly=not m.get("hasC"), requires=[], root=str(Path(m["path"]).parent))
        for m in info.get("modules") or []
    ]


def workspace_of(mp: Path) -> Path:
    return mp.parent


def find_board_dir(mp: Optional[Path], port: str, board: str) -> Optional[Path]:
    """Upstream's board directory (build_mp.py's boards are always upstream's)."""
    if not mp or not board:
        return None
    d = mp / "ports" / port / "boards" / board
    return d if d.is_dir() else None


def artifact_info(
    build_system: Path,
    port: str,
    board: str,
    variant: str,
    interpreter: str = "micropython",
    out_dir: str = "",
) -> dict:
    bdir = fb.expected_build_dir(
        build_system, port=port, board=board, variant=variant,
        interpreter=interpreter, out_dir=out_dir,
    )
    art = fb.find_artifact(bdir)
    if art:
        st = art.stat()
        info = {"ready": True, "artifact": str(art), "size": st.st_size,
                "mtime": st.st_mtime, "buildDir": str(bdir)}
    else:
        info = {"ready": False, "artifact": None, "buildDir": str(bdir)}
    # The default flash offset, for the UI to pre-fill. esp32 only.
    if port == "esp32" and interpreter == "micropython":
        mp = build_system / "micropython"
        info["flashOffset"] = esp32_flash_offset(
            mp / "ports" / "esp32", board, board_dir=find_board_dir(mp, port, board)
        )
    return info


def partition_override_path(workspace: Path, board: str, variant: str) -> Path:
    # Sibling of the micropython tree (workspace == micropython's parent), as
    # `firmware partitions` writes it. build_mp.py doesn't read it: it finds a
    # partitions.csv by its own convention and grows the app partition itself.
    name = board + (f"_{variant}" if variant else "")
    return workspace / "esp32_partitions" / f"{name}.csv"


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


def _make_args(ns: argparse.Namespace) -> list[str]:
    out: list[str] = []
    for a in getattr(ns, "make_arg", None) or []:
        out += a if isinstance(a, list) else [a]
    return out


def do_build(ns: argparse.Namespace) -> None:
    interpreter = _interpreter(ns)
    port = ns.port
    board = ns.board or ""
    variant = ns.variant or ""
    if (getattr(ns, "preset", "") or "").strip():
        emit_result(
            False,
            error="Presets are gone: name the modules instead, or `all` for every one "
                  "(mpftp firmware modules lists them).",
        )
        return
    try:
        bs = locate_build_system(ns)
    except fb.BuildSystemError as e:
        emit_result(False, error=str(e))
        return
    if bs is None:
        emit_result(False, error=fb.not_found_message())
        return
    emit_log(f"[mpftp] building with {bs / 'build_mp.py'}")

    env = fb.build_env(getattr(ns, "out_dir", "") or "", ns.jobs or 0)
    if interpreter == "micropython":
        # build_mp.py fetches ESP-IDF and emsdk itself, at its locked versions;
        # a cross-compiler on PATH is still ours to check, so a missing one
        # says what to install instead of failing deep in make.
        need, extra_bins = resolve_command_toolchains(port, ns)
        if need:
            emit_result(
                False,
                error=f"{need['label']} not found for the {port} build. {need['hint']}",
                needToolchain=need,
            )
            return
        if extra_bins:
            env["PATH"] = os.pathsep.join([str(d) for d in extra_bins] + [env.get("PATH", "")])
            emit_log(f"[mpftp] toolchain PATH += {os.pathsep.join(str(d) for d in extra_bins)}")
        if port == "esp32" and board:
            ovr = partition_override_path(bs.parent, board, variant)
            if ovr.is_file():
                emit_log(
                    f"[mpftp] note: {ovr} is not used. build_mp.py picks the partition "
                    "table and grows the app partition itself (--no-autosize refuses instead)."
                )

    try:
        argv = fb.build_argv(
            bs,
            port=port,
            board=board,
            variant=variant,
            modules=getattr(ns, "modules", "") or "",
            interpreter=interpreter,
            flash=getattr(ns, "flash", "") or "",
            clean=bool(ns.clean),
            autosize=getattr(ns, "autosize", True),
            make_args=_make_args(ns),
        )
    except fb.BuildSystemError as e:
        emit_result(False, error=str(e))
        return
    res = fb.run_build(argv, env, emit_log, port)
    ok = bool(res.pop("ok"))
    target = f"{interpreter}:{port}/{board}/{variant}"
    if not ok:
        emit_result(False, **res)
        log_activity("firmware_build", f"failed {target}", {"rc": res.get("returncode")})
        return
    if port == "esp32" and interpreter == "micropython":
        mp = bs / "micropython"
        res["flashOffset"] = esp32_flash_offset(
            mp / "ports" / "esp32", board, board_dir=find_board_dir(mp, port, board)
        )
    save_state(
        {
            "lastSelection": {
                "interpreter": interpreter,
                "port": port,
                "board": board,
                "variant": variant,
                "modules": getattr(ns, "modules", "") or "",
                "flash": getattr(ns, "flash", "") or "",
            }
        }
    )
    emit_result(True, buildSystem=str(bs), **res)
    log_activity("firmware_build", f"ok {target}", {"artifact": res.get("artifact")})


def do_clean(ns: argparse.Namespace) -> None:
    """Delete the target's build dir: what build_mp.py --clean does first."""
    try:
        bs = locate_build_system(ns)
    except fb.BuildSystemError as e:
        emit_result(False, error=str(e))
        return
    if bs is None:
        emit_result(False, error=fb.not_found_message())
        return
    bdir = fb.expected_build_dir(
        bs, port=ns.port, board=ns.board or "", variant=ns.variant or "",
        interpreter=_interpreter(ns), out_dir=getattr(ns, "out_dir", "") or "",
    )
    if bdir.is_dir():
        shutil.rmtree(bdir)
        emit_log(f"[mpftp] removed {bdir}")
    else:
        emit_log(f"[mpftp] nothing to clean: {bdir} does not exist")
    emit_result(True, buildDir=str(bdir))


# --------------------------------------------------------------------------- #
# Flash
# --------------------------------------------------------------------------- #

# Second-stage bootloader offset in flash by chip family. The merged
# firmware.bin starts at the bootloader, so this is where it is written when a
# board.json does not spell out deploy_options.flash_offset. These are ESP-IDF's
# CONFIG_BOOTLOADER_OFFSET_IN_FLASH defaults (components/bootloader/
# Kconfig.projbuild) and esptool's BOOTLOADER_FLASH_OFFSET per target: the P4
# and C5 keep their first two sectors for the key manager.
_BOOTLOADER_OFFSET_BY_MCU = {
    "esp32": "0x1000",
    "esp32s2": "0x1000",
    "esp32s3": "0x0",
    "esp32c2": "0x0",
    "esp32c3": "0x0",
    "esp32c5": "0x2000",
    "esp32c6": "0x0",
    "esp32c61": "0x0",
    "esp32h2": "0x0",
    "esp32p4": "0x2000",
}


# esp_chip_id_t, as an image header carries it (esp_app_format.h).
_MCU_BY_IMAGE_CHIP_ID = {
    0x0000: "esp32",
    0x0002: "esp32s2",
    0x0005: "esp32c3",
    0x0009: "esp32s3",
    0x000C: "esp32c2",
    0x000D: "esp32c6",
    0x0010: "esp32h2",
    0x0012: "esp32p4",
    0x0014: "esp32c61",
    0x0017: "esp32c5",
}


def esp32_image_family(artifact: Path) -> str:
    """The chip a merged firmware.bin was built for, from its first image
    header (magic 0xE9, chip_id at byte 12); "" when it isn't one."""
    try:
        with open(artifact, "rb") as f:
            head = f.read(16)
    except OSError:
        return ""
    if len(head) < 14 or head[0] != 0xE9:
        return ""
    return _MCU_BY_IMAGE_CHIP_ID.get(int.from_bytes(head[12:14], "little"), "")


def esp32_flash_offset_for_family(family: str) -> str:
    """Bootloader offset from MCU family string (e.g. Thonny catalog ``family``)."""
    mcu = (family or "").lower().replace("-", "")
    return _BOOTLOADER_OFFSET_BY_MCU.get(mcu, "0x0")


def esp32_flash_offset(
    port_dir: Path, board: str, family: str = "", board_dir: Optional[Path] = None
) -> str:
    """Resolve the esp32 flash offset for ``board``.

    Prefers ``board.json``'s ``deploy_options.flash_offset`` from:
      1. the board directory (``board_dir``, e.g. an overlay's), else the
         local MicroPython tree (``port_dir/boards/<board>/board.json``)
      2. upstream GitHub copy of the same file (download mode / no checkout)
    When that is absent, infers from ``board.json`` ``mcu`` / catalog ``family``
    — classic/S2 at 0x1000, P4 at 0x2000, newer parts at 0x0.
    """
    if board and (port_dir or board_dir):
        bj = (board_dir or port_dir / "boards" / board) / "board.json"
        try:
            data = json.loads(bj.read_text("utf-8"))
            explicit = data.get("deploy_options", {}).get("flash_offset")
            if explicit is not None and str(explicit).strip() != "":
                try:
                    from .firmware_download import normalize_flash_offset

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
            from .firmware_download import resolve_remote_flash_offset

            # Catalog / UI port is esp32 for all Espressif families; GitHub path
            # is always ports/esp32/boards/<BOARD>/board.json.
            remote = resolve_remote_flash_offset(board, port="esp32")
            if remote:
                return remote
            # board.json without deploy_options.flash_offset — use its mcu.
            from .firmware_download import fetch_board_json

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

# A binary partition table is 32-byte entries from _PARTITION_TABLE_OFFSET.
# Real entries start with the magic AA 50; the trailing checksum entry starts
# EB EB; the rest of the 0xC00 region is erase padding.
_PT_ENTRY_MAGIC = b"\xaa\x50"
_PT_MD5_MAGIC = b"\xeb\xeb"
_PT_ENTRY_SIZE = 32
_PT_REGION_SIZE = 0xC00

_PT_TYPES = {0: "app", 1: "data"}
_PT_SUBTYPES = {
    (0, 0x00): "factory",
    (0, 0x10): "ota_0",
    (0, 0x11): "ota_1",
    (0, 0x20): "test",
    (1, 0x00): "ota",
    (1, 0x01): "phy",
    (1, 0x02): "nvs",
    (1, 0x03): "coredump",
    (1, 0x04): "nvs_keys",
    (1, 0x05): "efuse",
    (1, 0x06): "undefined",
    (1, 0x80): "esphttpd",
    (1, 0x81): "fat",
    (1, 0x82): "spiffs",
    (1, 0x83): "littlefs",
}


def parse_partition_table(blob: bytes) -> list[dict[str, Any]]:
    """Decode a binary esp32 partition table into rows.

    Stops at the checksum entry or at the first thing that is not an entry, so
    it is safe to hand the whole 0xC00 region including its erase padding.
    """
    rows: list[dict[str, Any]] = []
    for off in range(0, max(0, len(blob) - _PT_ENTRY_SIZE + 1), _PT_ENTRY_SIZE):
        entry = blob[off : off + _PT_ENTRY_SIZE]
        if entry[:2] != _PT_ENTRY_MAGIC:
            break  # EB EB checksum row, or 0xFF padding: the table ends here
        ptype, subtype = entry[2], entry[3]
        p_offset, p_size = struct.unpack("<II", entry[4:12])
        label = entry[12:28].split(b"\x00", 1)[0].decode("utf-8", "replace")
        (flags,) = struct.unpack("<I", entry[28:32])
        rows.append(
            {
                "name": label,
                "type": _PT_TYPES.get(ptype, str(ptype)),
                "subtype": _PT_SUBTYPES.get((ptype, subtype), hex(subtype)),
                "offset": p_offset,
                "size": p_size,
                "flags": flags,
            }
        )
    return rows


def diff_partition_tables(
    want: list[dict[str, Any]], got: list[dict[str, Any]]
) -> list[str]:
    """Say, in sentences, every way the device's table differs from the image's.

    An empty list means identical. The point is to name the partition, the
    field and both values: "the tables differ" is what cost a night on a
    T-Embed whose vfs had moved 64 KB.
    """
    out: list[str] = []
    want_by_name = {r["name"]: r for r in want}
    got_by_name = {r["name"]: r for r in got}
    for row in want:
        name = row["name"]
        other = got_by_name.get(name)
        if other is None:
            out.append(f"{name}: in the image, absent on the device")
            continue
        for field in ("offset", "size"):
            if row[field] != other[field]:
                out.append(
                    f"{name}: {field} {hex(other[field])} on the device, "
                    f"{hex(row[field])} in the image"
                )
        for field in ("type", "subtype"):
            if row[field] != other[field]:
                out.append(
                    f"{name}: {field} {other[field]} on the device, "
                    f"{row[field]} in the image"
                )
    for row in got:
        if row["name"] not in want_by_name:
            out.append(f"{row['name']}: on the device, absent from the image")
    return out


def partition_table_from_image(artifact: Path, offset: Any = 0) -> Optional[bytes]:
    """Slice the partition table out of a whole-flash image, or None.

    A full esp32 image written at 0x0 carries its own table at 0x8000, so the
    layout can be checked from the ``.bin`` alone. That matters because the
    build directory that produced it is usually gone -- flashing a saved image
    with ``--artifact`` is exactly the case where the sibling
    ``partition_table/partition-table.bin`` does not exist, and where the check
    used to be skipped.
    """
    try:
        if int(str(offset), 0) != 0:
            return None  # a partial image does not contain the table
    except (TypeError, ValueError):
        return None
    try:
        with artifact.open("rb") as fh:
            fh.seek(_PARTITION_TABLE_OFFSET)
            blob = fh.read(_PT_REGION_SIZE)
    except OSError:
        return None
    if len(blob) < _PT_ENTRY_SIZE or blob[:2] != _PT_ENTRY_MAGIC:
        return None
    return blob


def _esptool_reset_mode(value: str, default: str) -> str:
    """Normalize esptool v5 reset-mode names (hyphens; accept legacy underscores)."""
    v = (value or "").strip().replace("_", "-")
    return v or default


def _read_device_partition_table(base: list[str], nbytes: int) -> Optional[bytes]:
    """Read the on-device partition-table region, or None if it can't be read.

    The read runs through the same esptool as the flash (Windows esptool under
    WSL), writing to a temp file whose path is translated for that host.

    Tried twice: ``default-reset`` first, which is what a board sitting at a
    REPL needs, then ``no-reset`` for a board already in ROM download mode,
    where toggling DTR/RTS can knock it back out. Reading the table is the
    whole point of flashing through the ROM port -- that is the route with no
    REPL to ask, so it is the one where a wrong layout goes unnoticed.
    """
    import tempfile
    tmp = Path(tempfile.gettempdir()) / f"mpftp_pt_{os.getpid()}.bin"
    out_arg = _wslpath_w(str(tmp)) if HOST == "wsl" else str(tmp)
    for before in ("default-reset", "no-reset"):
        cmd = base + [
            "--before",
            before,
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
            if r.returncode == 0 and tmp.is_file():
                return tmp.read_bytes()
        except Exception:
            pass
        finally:
            try:
                tmp.unlink()
            except Exception:
                pass
    return None


def _expected_partition_table(artifact: Path, offset: Any = 0) -> tuple[Optional[bytes], str]:
    """The table this flash will install, and where it was found.

    The build directory's ``partition_table/partition-table.bin`` is preferred
    because it is exactly the bytes the build produced; a whole-flash image
    carries the same table at 0x8000 and is the fallback, which is what makes
    the check work for a saved ``.bin`` flashed with ``--artifact``.
    """
    sibling = artifact.parent / "partition_table" / "partition-table.bin"
    if sibling.is_file():
        try:
            return sibling.read_bytes(), str(sibling)
        except OSError:
            pass
    blob = partition_table_from_image(artifact, offset)
    if blob is not None:
        return blob, f"{artifact.name} @ {hex(_PARTITION_TABLE_OFFSET)}"
    return None, ""


def _esp32_layout_check(
    base: list[str], artifact: Path, offset: Any = 0
) -> dict[str, Any]:
    """Compare the device's partition table with the one about to be flashed.

    Returns ``determined``, and when determined, ``changed`` plus a list of
    ``differences`` naming partition, field and both values. A moved
    vfs/storage offset leaves a stale filesystem that boots corrupt, and the
    board then sits in ``inisetup.fs_corrupted()`` before USB starts -- no
    panic, no console, nothing on the bus. Saying which partition moved is the
    difference between a minute and a night.
    """
    want_blob, source = _expected_partition_table(artifact, offset)
    if want_blob is None:
        return {
            "determined": False,
            "reason": "no partition table in the image or beside it",
        }
    got_blob = _read_device_partition_table(base, len(want_blob))
    if got_blob is None:
        return {"determined": False, "reason": "could not read the device's table"}
    want = parse_partition_table(want_blob)
    got = parse_partition_table(got_blob[: len(want_blob)])
    differences = diff_partition_tables(want, got)
    return {
        "determined": True,
        "changed": bool(differences) or got_blob[: len(want_blob)] != want_blob,
        "differences": differences,
        "source": source,
    }


# An ESP-IDF image starts with 0xE9. An application image carries its
# esp_app_desc_t at the start of its first segment: after the 24-byte image
# header and the 8-byte segment header, so at byte 32. A bootloader, and the
# combined firmware.bin that starts with one, has something else there.
_ESP_IMAGE_MAGIC = 0xE9
_ESP_APP_DESC_MAGIC = 0xABCD5432
_ESP_APP_DESC_AT = 32


def esp_image_is_app(artifact: Path) -> bool:
    """True when ``artifact`` is an ESP-IDF application image (micropython.bin)."""
    try:
        with artifact.open("rb") as fh:
            head = fh.read(_ESP_APP_DESC_AT + 4)
    except OSError:
        return False
    if len(head) < _ESP_APP_DESC_AT + 4 or head[0] != _ESP_IMAGE_MAGIC:
        return False
    return struct.unpack_from("<I", head, _ESP_APP_DESC_AT)[0] == _ESP_APP_DESC_MAGIC


def app_image_at_bootloader_error(artifact: Path, offset: Any) -> Optional[str]:
    """Why writing ``artifact`` at ``offset`` would brick the boot, or None.

    Everything below the partition table (0x8000) is the second-stage
    bootloader's. An application image written there is loaded by the ROM as a
    bootloader, so the board boot-loops on a watchdog reset, and the write
    runs over the partition table too. The image meant for that offset is the
    combined ``firmware.bin``, which starts with the bootloader.
    """
    try:
        at = int(str(offset), 0)
    except (TypeError, ValueError):
        return None
    if at >= _PARTITION_TABLE_OFFSET or not esp_image_is_app(artifact):
        return None
    combined = artifact.parent / "firmware.bin"
    hint = (
        f"Flash {combined} instead"
        if combined.is_file() and combined != artifact
        else "Flash the build's combined firmware.bin instead"
    )
    return (
        f"{artifact.name} is an application image, and {hex(at)} is the "
        "bootloader's offset: the board would boot-loop and lose its partition "
        f"table. {hint}, or pass the application partition's offset "
        "(usually 0x10000) with --offset."
    )


def flash_esp32(ns: argparse.Namespace, mp: Optional[Path], artifact: Path) -> None:
    port_dir = (mp / "ports" / ns.port) if mp else Path(".")
    family = getattr(ns, "family", "") or ""
    if not family and not ns.board:
        # An --artifact with no board: the image says which chip it is for. A
        # P4 image written at 0x0 instead of 0x2000 boot-loops (2026-10-05).
        family = esp32_image_family(artifact)
    offset = (getattr(ns, "offset", "") or "").strip() or esp32_flash_offset(
        port_dir,
        ns.board or "",
        family=family,
        board_dir=find_board_dir(mp, ns.port, ns.board or ""),
    )
    emit_log(f"[mpftp] flash offset {offset}")
    wrong_image = app_image_at_bootloader_error(artifact, offset)
    if wrong_image:
        emit_result(False, error=wrong_image)
        return
    fw = str(artifact)
    if HOST == "wsl":
        fw = _wslpath_w(fw)
    cmd = _esptool_cmd(ns)
    base = cmd + ["-b", str(ns.baud or 460800), "-p", ns.device]
    # The image names the chip it was built for. Telling esptool makes it
    # refuse a board that is a different chip ("This chip is ESP32-S3, not
    # ESP32") before it writes anything, rather than flashing an image the
    # board can't boot.
    chip = esp32_image_family(artifact)
    if chip in _BOOTLOADER_OFFSET_BY_MCU:
        base += ["--chip", chip]

    erase = getattr(ns, "erase", False)
    if not erase:
        # A moved vfs/storage offset leaves a stale filesystem that boots corrupt.
        # Never auto-erase: warn and require an explicit erase + second Flash.
        check = _esp32_layout_check(base, artifact, offset)
        if check.get("determined") and check.get("changed"):
            differences = check.get("differences") or []
            detail = "; ".join(differences) if differences else (
                "the tables differ byte for byte but name the same partitions"
            )
            emit_log(
                "[mpftp] partition layout on the device differs from this firmware: "
                + detail
            )
            emit_result(
                False,
                error=(
                    "Partition table on the device differs from this build: "
                    f"{detail}. Flashing anyway would leave the old filesystem "
                    "where the new table does not expect it, and the board can "
                    "boot into fs_corrupted() with nothing on the USB bus. "
                    "Re-flash with erase to apply the new table — that wipes the "
                    "filesystem (vfs/storage) partition, so copy anything you "
                    "want off the board first."
                ),
                needEraseConfirm={
                    "reason": "partition_layout_changed",
                    "message": (
                        "The on-device partition table does not match this firmware "
                        f"({detail}). Re-flashing with erase will apply the new table "
                        "but wipe the filesystem (vfs/storage) partition — all files "
                        "on the board will be lost."
                    ),
                    "differences": differences,
                },
            )
            return
        if check.get("determined"):
            emit_log(
                "[mpftp] partition layout matches (compared against "
                f"{check.get('source')})"
            )
        else:
            emit_log(
                "[mpftp] could not check the partition layout: "
                f"{check.get('reason')}"
            )

    before = _esptool_reset_mode(getattr(ns, "before", "") or "", "default-reset")
    after = _esptool_reset_mode(getattr(ns, "after", "") or "", "hard-reset")
    full = base + ["--before", before, "--after", after, "write-flash"]
    if erase:
        # One esptool run that erases and then writes, not an erase-flash run
        # followed by a write-flash run: the second run would have to reset
        # the board into its ROM loader again, and a native-USB board whose
        # app was just erased may not come back on the same port.
        emit_log("[mpftp] erasing all of flash before writing…")
        full.append("--erase-all")
    full += [offset, fw]
    rc = stream_process(full, Path.cwd(), dict(os.environ))
    if rc != 0:
        emit_result(False, error=f"esptool failed (exit {rc})")
        return
    emit_result(True, device=ns.device, offset=offset, artifact=str(artifact))
    log_activity("firmware_flash", f"esp32 {ns.board} -> {ns.device}", {"offset": offset})


def flash_uf2(ns: argparse.Namespace, artifact: Path) -> None:
    """Flash by copying a .uf2 onto a mounted bootloader volume.

    The copy is not the proof -- see ``uf2.wait_for_volume_gone``. Every exit
    path here reports what was actually observed, because the two ways this
    goes wrong (a copy that writes nothing, a bootloader that ignores an image
    it does not own) both look exactly like success from the host side.
    """
    try:
        meta = uf2.parse_uf2(artifact)
    except uf2.Uf2Error as e:
        emit_result(False, error=str(e), method="uf2")
        return
    except OSError as e:
        emit_result(False, error=f"Cannot read {artifact}: {e}", method="uf2")
        return

    families = "/".join(meta["family_names"]) or "none"
    emit_log(
        f"[mpftp] {artifact.name}: {meta['blocks']} blocks, "
        f"{meta['payload_bytes']} bytes, family {families}"
    )
    for warning in meta["warnings"]:
        emit_log(f"[mpftp] warning: {warning}")

    volume, ambiguous = _select_uf2_volume(ns)
    if ambiguous:
        emit_result(False, method="uf2", error=ambiguous)
        return
    if volume is None:
        _uf2_no_volume(ns, artifact)
        return

    root = Path(volume["path"])
    board_id = volume.get("board_id") or "unknown"
    emit_log(f"[mpftp] bootloader volume {root} (Board-ID: {board_id})")

    dest = root / artifact.name
    emit_phase("flashing", f"copying {artifact.name} -> {root}")
    try:
        written = uf2.copy_uf2(artifact, dest)
    except OSError as e:
        # A write error part-way through can also mean the board rebooted early.
        # Check before blaming the copy.
        if uf2.wait_for_volume_gone(root, timeout=2.0):
            emit_log(f"[mpftp] write ended with {e}, but the volume went away")
            _uf2_success(ns, artifact, root, board_id, meta, written=-1)
            return
        emit_result(
            False,
            method="uf2",
            device=str(root),
            artifact=str(artifact),
            error=f"Copy to {dest} failed: {e}",
        )
        return

    expected = artifact.stat().st_size
    if written != expected:
        emit_result(
            False,
            method="uf2",
            device=str(root),
            artifact=str(artifact),
            error=f"Short write: {written} of {expected} bytes reached {dest}.",
        )
        return
    emit_log(f"[mpftp] wrote {written} bytes; waiting for the board to reboot")

    timeout = float(getattr(ns, "uf2_timeout", 0) or UF2_REBOOT_TIMEOUT)
    if uf2.wait_for_volume_gone(root, timeout=timeout):
        _uf2_success(ns, artifact, root, board_id, meta, written)
        return

    # The volume is still mounted, so the bootloader did not accept the image.
    # A family the board does not own is by far the most common cause: those
    # blocks are skipped in silence, leaving a perfectly successful copy behind.
    hint = (
        f"The image is {families}; check it matches this board."
        if meta["families"]
        else "This UF2 carries no family ID, so it cannot be matched to the board."
    )
    emit_result(
        False,
        method="uf2",
        device=str(root),
        artifact=str(artifact),
        family=meta["family_names"],
        board_id=board_id,
        error=(
            f"Copied {written} bytes to {dest}, but {root} is still mounted after "
            f"{timeout:.0f}s -- the bootloader did not accept the image. {hint}"
        ),
    )


def _uf2_success(ns: argparse.Namespace, artifact: Path, root: Path, board_id: str,
                 meta: dict, written: int) -> None:
    emit_log(f"[mpftp] {root} unmounted -- board rebooted into the new firmware")
    emit_result(
        True,
        method="uf2",
        device=str(root),
        artifact=str(artifact),
        board_id=board_id,
        family=meta["family_names"],
        bytes_written=written,
        blocks=meta["blocks"],
    )
    log_activity("firmware_flash", f"uf2 -> {root}", {"artifact": str(artifact)})


def _select_uf2_volume(ns: argparse.Namespace) -> tuple[Optional[dict], Optional[str]]:
    """Resolve which bootloader volume to write to.

    Returns ``(volume, error)``. Reporting is left to the caller so that exactly
    one result line is ever emitted -- the streaming protocol requires the
    result to be both unique and last.

    ``(None, None)`` means no volume was found, which is recoverable (picotool);
    ``(None, message)`` means the choice was ambiguous, which is not.
    """
    device = getattr(ns, "device", "") or ""
    if device and uf2.looks_like_volume(device):
        root = Path(device)
        return {"path": device,
                "board_id": uf2.read_volume_info(root).get("Board-ID", "")}, None

    volumes = uf2.find_uf2_volumes(HOST)
    if len(volumes) == 1:
        return volumes[0], None
    if len(volumes) > 1:
        # Never guess: the wrong choice silently overwrites the firmware on a
        # board the caller did not name.
        listing = ", ".join(f"{v['path']} ({v['board_id'] or 'unknown'})" for v in volumes)
        return None, (f"Multiple UF2 bootloader volumes mounted: {listing}. "
                      "Pass --device with the one to flash.")
    return None, None


def _uf2_no_volume(ns: argparse.Namespace, artifact: Path) -> None:
    """No bootloader volume: try picotool for rp2, else explain how to get one."""
    if getattr(ns, "port", "") == "rp2" and shutil.which("picotool"):
        emit_log("[mpftp] no UF2 volume; trying picotool load")
        rc = stream_process(
            ["picotool", "load", "-f", "-x", str(artifact)], Path.cwd(), dict(os.environ)
        )
        emit_result(rc == 0, method="picotool", artifact=str(artifact),
                    error=None if rc == 0 else f"picotool failed (exit {rc})")
        return
    emit_result(
        False,
        method="uf2",
        error="No UF2 bootloader volume found. Put the board in bootloader mode "
              "(double-tap reset, hold BOOTSEL, or `mpftp bootloader`) and retry, "
              "or pass --device with the volume path. On WSL a removable drive is "
              "often not mounted under /mnt -- the Windows path (e.g. D:\\) works.",
    )


def do_flash(ns: argparse.Namespace) -> None:
    port = ns.port
    board = ns.board or ""
    variant = ns.variant or ""
    # --uf2 is what makes this reachable for ports with no entry in FLASHERS:
    # a board is UF2-flashable because a bootloader is running on it, which is a
    # property of the board's provisioning rather than of its MicroPython port.
    # An esp32 carrying tinyuf2 is the case that matters.
    force_uf2 = bool(getattr(ns, "uf2", False))
    if port not in FLASHERS and not force_uf2:
        emit_result(False, error=f"Flashing not supported for port '{port}'.")
        return
    mp: Optional[Path] = None
    if getattr(ns, "mp", None):
        mp = Path(ns.mp).expanduser().resolve()
    try:
        bs = locate_build_system(ns)
    except fb.BuildSystemError as e:
        if not ns.artifact:
            emit_result(False, error=str(e))
            return
        bs = None
    if mp is None and bs is not None and (bs / "micropython" / "ports").is_dir():
        mp = (bs / "micropython").resolve()  # its board.json gives the flash offset
    if ns.artifact:
        artifact = Path(ns.artifact).expanduser()
    else:
        if bs is None:
            emit_result(False, error="No --artifact, and no build to flash. " + fb.not_found_message())
            return
        info = artifact_info(
            bs, port, board, variant, _interpreter(ns), getattr(ns, "out_dir", "") or ""
        )
        if not info["ready"]:
            emit_result(
                False,
                error=f"No build found for this selection in {info['buildDir']}. Build first.",
            )
            return
        artifact = Path(info["artifact"])
    if not artifact.is_file():
        emit_result(False, error=f"Artifact not found: {artifact}")
        return

    if force_uf2:
        if artifact.suffix.lower() != ".uf2":
            emit_result(
                False,
                error=f"--uf2 needs a .uf2 artifact, got {artifact.name}. "
                      "Build a UF2-capable target, or drop --uf2 to flash over serial.",
            )
            return
        flash_uf2(ns, artifact)
    elif port == "esp32":
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
    # An in-port build dir from a make run by hand, if there is one.
    candidates.append(port_dir / (f"build-{board}-{variant}" if variant else f"build-{board}") / "sdkconfig")
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


def do_ptable(ns: argparse.Namespace) -> None:
    """Print a firmware image's partition table, and diff it on request.

    The table is 32-byte entries from 0x8000, magic AA 50. Having this as one
    command is what would have caught a T-Embed image whose vfs sat 64 KB off
    in seconds instead of several trips to the BOOT button.
    """
    image = Path(ns.image).expanduser()
    blob = partition_table_from_image(image, 0)
    if blob is None:
        print_json(
            {
                "error": f"no partition table at {hex(_PARTITION_TABLE_OFFSET)} in {image}",
                "hint": "ptable wants a whole-flash image (the .bin written at 0x0)",
            }
        )
        raise SystemExit(1)
    result: dict[str, Any] = {"image": str(image), "rows": parse_partition_table(blob)}

    if ns.compare:
        other_path = Path(ns.compare).expanduser()
        other = partition_table_from_image(other_path, 0)
        if other is None:
            result["compareError"] = f"no partition table in {other_path}"
        else:
            other_rows = parse_partition_table(other)
            result["compare"] = str(other_path)
            result["compareRows"] = other_rows
            result["differences"] = diff_partition_tables(
                result["rows"], other_rows
            )

    if ns.device:
        base = _esptool_cmd(ns) + ["-b", str(ns.baud or 460800), "-p", ns.device]
        got = _read_device_partition_table(base, len(blob))
        if got is None:
            result["deviceError"] = "could not read the device's table"
        else:
            device_rows = parse_partition_table(got[: len(blob)])
            result["device"] = ns.device
            result["deviceRows"] = device_rows
            result["deviceDifferences"] = diff_partition_tables(
                result["rows"], device_rows
            )
    print_json(result)


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
                # the MicroPython interpreter probe fills that in when available.
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
        elif isinstance(memfree, (int, float)) and memfree > 1_000_000 and has("SPIRAM"):
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
    try:
        bs = locate_build_system(ns)
    except fb.BuildSystemError:
        bs = None
    result = {
        "host": HOST,
        "micropython": str(mp) if mp else None,
        "workspace": str(workspace) if workspace else None,
        "buildSystem": str(bs) if bs else None,
        "state": load_state(),
    }
    if mp:
        save_state({"micropythonPath": str(mp)})
    print_json(result)


def _offer(ns: argparse.Namespace) -> tuple[Optional[Path], dict]:
    """The checkout and what its build_mp.py offers, or an {"error": ...}."""
    try:
        bs = locate_build_system(ns)
        if bs is None:
            return None, {"error": fb.not_found_message()}
        return bs, fb.offer(bs, _interpreter(ns))
    except (fb.BuildSystemError, subprocess.TimeoutExpired) as e:
        return None, {"error": str(e)}


def do_tree(ns: argparse.Namespace) -> None:
    bs, info = _offer(ns)
    if "error" in info:
        print_json({"error": info["error"], "ports": []})
        return
    print_json(
        {
            "buildSystem": str(bs),
            "interpreter": info["interpreter"],
            "micropython": info.get("micropython"),
            "circuitpython": info.get("circuitpython"),
            "workspace": str(bs.parent),
            "flashSizes": info.get("flashSizes") or [],
            "overlays": [],
            "ports": offer_ports(info),
        }
    )


def do_modules(ns: argparse.Namespace) -> None:
    bs, info = _offer(ns)
    if "error" in info:
        print_json({"error": info["error"], "roots": [], "modules": [], "presets": [], "overlays": []})
        return
    print_json(
        {
            "buildSystem": str(bs),
            "interpreter": info["interpreter"],
            "roots": [str(bs / "modules")],
            "modules": offer_modules(info),
            "presets": [],
            "overlays": [],
        }
    )


def do_artifact(ns: argparse.Namespace) -> None:
    try:
        bs = locate_build_system(ns)
    except fb.BuildSystemError as e:
        print_json({"ready": False, "error": str(e)})
        return
    if bs is None:
        print_json({"ready": False, "error": fb.not_found_message()})
        return
    print_json(
        artifact_info(
            bs,
            ns.port,
            ns.board or "",
            ns.variant or "",
            _interpreter(ns),
            getattr(ns, "out_dir", "") or "",
        )
    )


def do_download_tree(ns: argparse.Namespace) -> None:
    from .firmware_download import catalog_tree

    force = bool(getattr(ns, "force", False))
    print_json(catalog_tree(force=force))


def do_download_list(ns: argparse.Namespace) -> None:
    from .firmware_download import list_board

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
        raise SystemExit(1) from e


def do_download(ns: argparse.Namespace) -> None:
    from .firmware_download import download_file, find_variant, load_catalog, pick_download

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
            uf2=bool(getattr(ns, "uf2", False)),
        )
        emit_log(f"[mpftp] downloading {chosen['url']}")
        path = download_file(chosen["url"], progress=progress)
        st = path.stat()
        from .firmware_download import resolve_remote_flash_offset

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
    p = argparse.ArgumentParser(prog="mpftp.firmware", description="mpftp firmware engine")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_mp(sp: argparse.ArgumentParser, required: bool = False) -> None:
        sp.add_argument("--mp", default=None, required=required, help="MicroPython tree path")
        sp.add_argument(
            "--workspace",
            default=None,
            help="Editor workspace folder(s), os.pathsep-joined; used to find micropython/",
        )
        sp.add_argument("--idf", default=None,
                        help="ESP-IDF path (not used by builds: build_mp.py brings its own)")
        sp.add_argument("--emsdk", default=None,
                        help="emsdk path (not used by builds: build_mp.py brings its own)")
        sp.add_argument("--build-system", dest="build_system", default=None,
                        help="micropython-pydevices checkout whose build_mp.py builds")
        sp.add_argument("--interpreter", choices=fb.INTERPRETERS, default="micropython",
                        help="circuitpython: CircuitPython-compatible firmware, from its own ports and boards")
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

    for name in ("modules", "cmods"):
        c = sub.add_parser(name)
        add_mp(c)
        c.set_defaults(func=do_modules)

    def add_target(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--port", required=True)
        sp.add_argument("--board", default="")
        sp.add_argument("--variant", default="")
        sp.add_argument("--out-dir", dest="out_dir", default="",
                        help="where builds go (build_mp.py's OUT_DIR); default: the checkout's builds/")

    a = sub.add_parser("artifact")
    add_mp(a)
    add_target(a)
    a.set_defaults(func=do_artifact)

    b = sub.add_parser("build")
    add_mp(b)
    add_target(b)
    b.add_argument("--preset", default="", help=argparse.SUPPRESS)  # gone; refused with a hint
    b.add_argument("--modules", default="",
                   help='comma list for build_mp.py: short names, full paths, or "all"')
    b.add_argument("--flash", default="", help="esp32 flash size, e.g. 16MB")
    b.add_argument("--make-arg", dest="make_arg", action="append", default=[],
                   help="argument for make, e.g. CIRCUITPY_ULAB=0 (repeatable)")
    b.add_argument("--clean", action="store_true", help="delete this target's build dir first")
    b.add_argument("--jobs", type=int, default=0, help="parallel jobs (build_mp.py's JOBS)")
    b.add_argument("--no-autosize", dest="autosize", action="store_false", default=True,
                   help="esp32: refuse instead of growing the app partition")
    b.set_defaults(func=do_build)

    cl = sub.add_parser("clean")
    add_mp(cl)
    add_target(cl)
    cl.set_defaults(func=do_clean)

    f = sub.add_parser("flash")
    add_mp(f, required=False)  # optional when --artifact is a downloaded file
    add_target(f)
    f.add_argument("--family", default="", help="MCU family for flash offset (download mode)")
    f.add_argument("--device", default="")
    f.add_argument("--artifact", default="")
    f.add_argument("--baud", type=int, default=460800)
    f.add_argument("--offset", default="",
                   help="esp32 flash offset override (default: board.json / chip family)")
    f.add_argument("--erase", action="store_true")
    f.add_argument("--uf2", action="store_true",
                   help="force the UF2 copy path (any port with a bootloader volume)")
    f.add_argument("--uf2-timeout", dest="uf2_timeout", type=float, default=0.0,
                   help=f"seconds to wait for the volume to unmount (default {UF2_REBOOT_TIMEOUT:.0f})")
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
    dld.add_argument(
        "--uf2",
        action="store_true",
        help="prefer .uf2 (default: .bin for esp32, .uf2 for rp2/samd)",
    )
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
                    help="JSON of MicroPython interpreter hints (optional enrichment)")
    dt.set_defaults(func=do_detect)

    ptb = sub.add_parser("ptable", help="Print an image's partition table; diff it")
    ptb.add_argument("image", help="Firmware .bin (whole-flash image)")
    ptb.add_argument("--compare", default="", help="Second image to diff against")
    ptb.add_argument("--device", default="", help="Also read and diff this board's table")
    ptb.add_argument("--baud", type=int, default=460800)
    ptb.add_argument("--esptool", default="")
    ptb.set_defaults(func=do_ptable)

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
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
