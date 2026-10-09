"""Firmware builds through micropython-pydevices' ``build_mp.py``.

``mpftp firmware build`` doesn't run ``make`` itself. It finds a
micropython-pydevices checkout, asks its ``build_mp.py`` what it can build
(ports, boards, variants, modules), and runs it with your choices, so a build
from mpftp is the same build you would get typing the command yourself.

Everything here is stdlib only: the firmware engine (``mpftp.firmware``)
imports it, and the engine runs under whatever python started the CLI.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Union

#: What install.sh clones into, and what a sibling checkout is called.
CHECKOUT_NAME = "micropython-pydevices"
INSTALL_COMMAND = "curl -fsSL https://pydevices.github.io/install.sh | sh"
INTERPRETERS = ("micropython", "circuitpython")

#: build_mp.py's own name for a build's last directory level when no
#: --variant is given: what upstream's make calls it (build-plan.md, "Output").
DEFAULT_VARIANT = {"unix": "standard", "windows": "standard", "webassembly": "standard"}

#: Files a build leaves, flashable first.
ARTIFACT_NAMES = (
    "firmware.uf2",
    "firmware.bin",
    "firmware.hex",
    "micropython.bin",
    "micropython.exe",
    "micropython.mjs",
    "micropython",
)


class BuildSystemError(RuntimeError):
    """build_mp.py couldn't be found, or couldn't say what it offers."""


# --------------------------------------------------------------------------- #
# Finding the checkout
# --------------------------------------------------------------------------- #

def is_build_system(p: Path) -> bool:
    return (p / "build_mp.py").is_file()


def _roots(workspace: Union[str, Iterable[str], None]) -> list[Path]:
    if not workspace:
        return []
    parts = workspace.split(os.pathsep) if isinstance(workspace, str) else list(workspace)
    return [Path(p).expanduser() for p in parts if str(p).strip()]


def find_build_system(
    hint: Optional[str] = None,
    *,
    configured: Optional[str] = None,
    mp: Optional[str] = None,
    workspace: Union[str, Iterable[str], None] = None,
) -> Optional[Path]:
    """The micropython-pydevices checkout to build with.

    Order: an explicit path; the ``buildSystemPath`` setting
    (``MPFTP_BUILD_SYSTEM``); each workspace root, as the checkout itself or
    holding one; beside a MicroPython checkout (``micropython/`` inside the
    checkout, or a sibling of it); ``~/micropython-pydevices``.

    An explicit path that isn't a checkout is an error, never a fallback, so a
    typo can't quietly build with some other checkout.
    """
    if hint:
        p = Path(hint).expanduser()
        if not is_build_system(p):
            raise BuildSystemError(f"no build_mp.py in {p}")
        return p.resolve()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    for root in _roots(workspace):
        candidates += [root, root / CHECKOUT_NAME]
    if mp:
        m = Path(mp).expanduser()
        for base in (m, m.resolve()):
            candidates += [base.parent, base.parent / CHECKOUT_NAME]
    candidates.append(Path.home() / CHECKOUT_NAME)
    for c in candidates:
        try:
            if is_build_system(c):
                return c.resolve()
        except OSError:
            continue
    return None


def not_found_message() -> str:
    return (
        "No micropython-pydevices checkout found: mpftp builds firmware with its "
        f"build_mp.py. Install one with `{INSTALL_COMMAND}`, then run mpftp from "
        "the folder that holds it, or pass --build-system PATH (or set "
        "buildSystemPath in ~/.mpftp/config.json, or MPFTP_BUILD_SYSTEM)."
    )


# --------------------------------------------------------------------------- #
# What build_mp.py offers
#
# build_mp.py has no listing mode of its own; its interactive prompts are made
# from a handful of functions (ports(), boards(), variants(), module_choices()
# and the cp_ twins). The lister below loads build_mp.py without running its
# main() and calls those, in a child process so nothing it does to sys.path or
# the environment reaches the engine.
# --------------------------------------------------------------------------- #

LISTER = r'''
import contextlib, io, json, os, runpy, sys

path, interpreter = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.dirname(path))
out = {"interpreter": interpreter}


def need(g, *names):
    missing = [n for n in names if n not in g]
    if missing:
        raise RuntimeError(
            "this build_mp.py has no " + ", ".join(missing)
            + "(); update micropython-pydevices (git pull)")


def safe(fn, *a):
    try:
        return list(fn(*a))
    except OSError:
        return []


def has_c(d):
    # modules/manifest.py's rule: a C half at the root, or in code/ (ulab).
    for c in (d, d / "code"):
        if (c / "micropython.mk").is_file() or (c / "micropython.cmake").is_file():
            return True
    try:
        return "c_module(" in (d / "manifest.py").read_text("utf-8", "replace")
    except OSError:
        return False


def modules(g):
    need(g, "MODULES_DIR", "module_choices")
    md = g["MODULES_DIR"]
    names = set(g["module_choices"]())
    try:
        names |= {row[0] for row in g["read_lock"]("modules.lock")}
    except Exception:
        pass
    recs = []
    for n in sorted(names, key=lambda n: (n != "all", n)):
        d = md / n
        recs.append({
            "name": n,
            "path": str(d),
            "present": d.is_dir(),
            "optIn": (d / "OPT_IN").is_file(),
            "hasC": n == "all" or has_c(d),
        })
    return recs


def micropython(g):
    need(g, "MP", "ports", "is_board_port", "boards", "variants")
    mp = g["MP"]
    if not (mp / "ports").is_dir() and "workspace" in g and "ensure_micropython" in g:
        ws = g["workspace"]()
        if ws:  # only a link to the sibling checkout; never a clone from a listing
            g["ensure_micropython"](ws)
    if not (mp / "ports").is_dir():
        raise RuntimeError(
            "micropython/ isn't in the checkout yet; build_mp.py fetches it on its "
            "first build (try: mpftp firmware build --port unix)")
    ports = []
    for port in g["ports"]():
        if g["is_board_port"](port):
            boards = [{"board": b, "variants": safe(g["variants"], port, b)} for b in g["boards"](port)]
            ports.append({"port": port, "kind": "boards", "boards": boards, "variants": []})
        else:
            vs = safe(g["variants"], port, None)
            ports.append({"port": port, "kind": "variants" if vs else "plain", "boards": [], "variants": vs})
    out["micropython"] = str(mp.resolve())
    out["flashSizes"] = list(g.get("FLASH_SIZES", ()))
    return ports


def circuitpython(g):
    need(g, "CP", "cp_ports", "cp_boards", "cp_variants")
    cp = g["CP"]
    if not (cp / "ports").is_dir():
        raise RuntimeError(
            "CircuitPython isn't fetched yet; build_mp.py clones it into "
            "deps/circuitpython on the first --interpreter circuitpython build")
    ours = g.get("CP_BOARDS")
    ports = []
    for port in g["cp_ports"]():
        names = set(g["cp_boards"](port))
        if ours is not None:  # laid into the checkout at build time
            names |= {p.parent.name for p in (ours / port).glob("*/mpconfigboard.mk")}
        if names:
            boards = [{"board": b, "variants": []} for b in sorted(names)]
            ports.append({"port": port, "kind": "boards", "boards": boards, "variants": []})
        else:
            vs = safe(g["cp_variants"], port)
            ports.append({"port": port, "kind": "variants" if vs else "plain", "boards": [], "variants": vs})
    out["circuitpython"] = str(cp.resolve())
    out["flashSizes"] = []
    return ports


try:
    with contextlib.redirect_stdout(io.StringIO()):
        g = runpy.run_path(path, run_name="build_mp")
        out["ports"] = circuitpython(g) if interpreter == "circuitpython" else micropython(g)
        out["modules"] = modules(g)
        out["defaultVariant"] = dict(g.get("DEFAULT_VARIANT", {}))
except SystemExit as e:  # build_mp.py's die()
    msg = str(e.code)
    out["error"] = msg[len("build_mp.py: "):] if msg.startswith("build_mp.py: ") else msg
except Exception as e:
    out["error"] = str(e) or type(e).__name__
print(json.dumps(out))
'''


def offer(build_system: Path, interpreter: str = "micropython", python: Optional[str] = None) -> dict:
    """Ports (with their boards and variants) and modules build_mp.py offers.

    Raises BuildSystemError with build_mp.py's own words when it can't say.
    """
    if interpreter not in INTERPRETERS:
        raise BuildSystemError(f"--interpreter takes {' or '.join(INTERPRETERS)}, not {interpreter!r}")
    script = Path(build_system) / "build_mp.py"
    r = subprocess.run(
        [python or sys.executable, "-c", LISTER, str(script), interpreter],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=120,
    )
    try:
        info = json.loads(r.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        tail = (r.stderr.strip() or r.stdout.strip()).splitlines()[-1:] or [f"exit {r.returncode}"]
        raise BuildSystemError(f"{script} could not list what it builds: {tail[0]}") from None
    if info.get("error"):
        raise BuildSystemError(f"build_mp.py: {info['error']}")
    info["buildSystem"] = str(build_system)
    return info


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #

def module_spec(modules: Union[str, Iterable[str], None]) -> str:
    """The --modules value: a comma list, with paths made absolute.

    build_mp.py resolves a relative path against its own working directory;
    mpftp makes it absolute first, so ``--modules ./earful`` means the folder
    you typed it in.
    """
    if modules is None:
        return ""
    items = modules.split(",") if isinstance(modules, str) else list(modules)
    out = []
    for item in (str(i).strip() for i in items):
        if not item:
            continue
        if "/" in item or os.sep in item or item.startswith("~"):
            item = str(Path(item).expanduser().resolve())
        if item not in out:
            out.append(item)
    return ",".join(out)


def build_argv(
    build_system: Path,
    *,
    port: str,
    board: str = "",
    variant: str = "",
    modules: Union[str, Iterable[str], None] = "",
    interpreter: str = "micropython",
    flash: str = "",
    clean: bool = False,
    autosize: bool = True,
    make_args: Iterable[str] = (),
    python: Optional[str] = None,
) -> list[str]:
    """The build_mp.py command line for a selection.

    --modules is always given, even empty, because build_mp.py asks for it
    otherwise and mpftp never gives it a terminal to ask on.
    """
    if interpreter not in INTERPRETERS:
        raise BuildSystemError(f"--interpreter takes {' or '.join(INTERPRETERS)}, not {interpreter!r}")
    if not port:
        raise BuildSystemError("--port is required (mpftp firmware list shows the ports)")
    argv = [python or sys.executable, str(Path(build_system) / "build_mp.py")]
    if interpreter != "micropython":
        argv += ["--interpreter", interpreter]
    argv += ["--port", port]
    if board:
        argv += ["--board", board]
    if variant:
        argv += ["--variant", variant]
    argv.append(f"--modules={module_spec(modules)}")
    if flash:
        argv += ["--flash", flash]
    if not autosize:
        argv.append("--no-autosize")
    if clean:
        argv.append("--clean")
    argv += [str(a) for a in make_args]
    return argv


def build_env(out_dir: str = "", jobs: int = 0, base: Optional[dict] = None) -> dict:
    env = dict(os.environ if base is None else base)
    env["PYTHONUNBUFFERED"] = "1"
    if out_dir:
        env["OUT_DIR"] = str(Path(out_dir).expanduser().resolve())
    if jobs:
        env["JOBS"] = str(jobs)
    return env


def output_root(build_system: Path, out_dir: str = "", env: Optional[dict] = None) -> Path:
    """Where build_mp.py puts builds: OUT_DIR, else the checkout's builds/."""
    chosen = out_dir or (env if env is not None else os.environ).get("OUT_DIR", "")
    return Path(chosen).expanduser().resolve() if chosen else Path(build_system) / "builds"


def expected_build_dir(
    build_system: Path,
    *,
    port: str,
    board: str = "",
    variant: str = "",
    interpreter: str = "micropython",
    out_dir: str = "",
) -> Path:
    """The directory build_mp.py builds this target into (build-plan.md, "Output")."""
    root = output_root(build_system, out_dir)
    if interpreter == "circuitpython":
        return root / "circuitpython" / port / (board or variant or "coverage")
    d = root / port
    if board:
        d = d / board
    return d / (variant or DEFAULT_VARIANT.get(port, "default"))


def find_artifact(bdir: Optional[Path]) -> Optional[Path]:
    if not bdir or not bdir.is_dir():
        return None
    for name in ARTIFACT_NAMES:
        if (bdir / name).is_file():
            return bdir / name
    return None


# What build_mp.py asks for when an argument is missing, and the flag that
# answers it. mpftp gives it no terminal, so it stops with "<prompt>: not
# given, and there is no terminal to ask on".
_PROMPT_FLAGS = {
    "Port": "--port",
    "CircuitPython port": "--port",
    "Board": "--board",
    "Variant": "--variant",
    "--modules": "--modules",
}
_NOT_GIVEN = re.compile(r"^(.*?):* not given, and there is no terminal to ask on$")

# A compiler's, linker's or make's own complaint, for the line that says why
# make failed.
_COMPILE_ERROR = re.compile(
    r"(: (fatal )?error: |: undefined reference to |^make(\[\d+\])?: \*\*\* |CMake Error|^\S+Error: )"
)


def explain(message: str, port: str = "") -> str:
    """build_mp.py's own error, with the mpftp flag that fixes a missing one."""
    m = _NOT_GIVEN.match(message)
    if m:
        what = m.group(1).strip().rstrip(":")  # its prompts end in ":" ("Board::")
        flag = _PROMPT_FLAGS.get(what) or _PROMPT_FLAGS.get(what.split(" (")[0]) or what
        where = f"mpftp firmware list --port {port}" if port and flag != "--port" else "mpftp firmware list"
        return f"build_mp.py needs {flag} (see `{where}`)"
    return message


class BuildOutput:
    """Reads build_mp.py's output as it streams: its errors, and where it built."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.first_failure: Optional[str] = None
        self.built_into: Optional[Path] = None
        self.last_line = ""

    def feed(self, line: str) -> None:
        s = line.rstrip("\r\n")
        if s.strip():
            self.last_line = s.strip()
        if s.startswith("build_mp.py: "):
            self.errors.append(s[len("build_mp.py: "):])
        elif s.startswith("Built into "):
            self.built_into = Path(s[len("Built into "):].strip())
        elif self.first_failure is None and _COMPILE_ERROR.search(s):
            self.first_failure = s.strip()

    def result(self, rc: int, port: str = "") -> dict:
        if rc == 0:
            info: dict[str, Any] = {"ok": True, "buildDir": str(self.built_into) if self.built_into else None}
            art = find_artifact(self.built_into)
            if art:
                st = art.stat()
                info.update(ready=True, artifact=str(art), size=st.st_size, mtime=st.st_mtime)
            else:
                info.update(ready=False, artifact=None)
            return info
        if self.errors:
            error = explain(self.errors[-1], port)
        else:  # a crash rather than a refusal: its last line is the exception
            error = f"build_mp.py stopped (exit {rc}): {self.last_line or 'no output'}"
        out: dict[str, Any] = {"ok": False, "error": error, "returncode": rc}
        if self.first_failure and self.first_failure not in error:
            out["detail"] = self.first_failure
        return out


def run_build(
    argv: list[str], env: dict, emit_log: Callable[[str], None], port: str = ""
) -> dict:
    """Run build_mp.py, streaming its output, and say how it went."""
    emit_log("$ " + " ".join(_quote(a) for a in argv))
    seen = BuildOutput()
    with subprocess.Popen(
        argv,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        errors="replace",
    ) as proc:
        assert proc.stdout
        for line in proc.stdout:
            emit_log(line)
            seen.feed(line)
        rc = proc.wait()
    return seen.result(rc, port)


def _quote(a: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./=:,+-]+", a):
        return a
    return "'" + a.replace("'", "'\\''") + "'"
