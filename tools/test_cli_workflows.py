#!/usr/bin/env python3
"""Exercise mpftp CLI workflows against attached boards.

Host-only commands run once. Board commands run on each REPL port from
``mpftp ports`` (or ``--device``). Work is isolated under ``/mpftp_cli_test``
on the board and a temp directory on the host.

Skipped by default (flashing or would leave the board in a flash/bootloader
state, or they mutate a firmware tree / download a full image):

- ``firmware flash`` (and ``--uf2``)
- ``firmware build`` / ``firmware clean``
- ``firmware download`` (binary fetch; catalog is covered by download-tree)
- ``bootloader``
- ``romfs deploy``

Pass ``--install-latest-firmware`` to download the latest official release for
each board (``.bin`` for esp32 serial, ``.uf2`` for rp2/samd) and flash it
with erase. That **wipes the board**.

Run from anywhere:

    python3 tools/test_cli_workflows.py
    python3 tools/test_cli_workflows.py --device COM4
    python3 tools/test_cli_workflows.py --device COM17 --install-latest-firmware
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

REPO = Path(__file__).resolve().parent.parent
MPFTP = REPO / "scripts" / "mpftp"
BOARD_ROOT = "/mpftp_cli_test"

Check = Callable[["StepResult"], Optional[str]]


@dataclass
class StepResult:
    name: str
    argv: list[str]
    status: str  # PASS FAIL SKIP
    detail: str = ""
    seconds: float = 0.0
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    parsed: Any = None


@dataclass
class Report:
    steps: list[StepResult] = field(default_factory=list)

    def add(self, step: StepResult) -> StepResult:
        self.steps.append(step)
        mark = {"PASS": ".", "FAIL": "F", "SKIP": "s"}[step.status]
        extra = f"  {step.detail}" if step.detail else ""
        print(f"{mark} {step.name} ({step.seconds:.1f}s){extra}", flush=True)
        return step

    def counts(self) -> tuple[int, int, int]:
        n = {"PASS": 0, "FAIL": 0, "SKIP": 0}
        for s in self.steps:
            n[s.status] += 1
        return n["PASS"], n["FAIL"], n["SKIP"]


def parse_json(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        start_l = text.find("[")
        if start_l != -1 and (start == -1 or start_l < start):
            start = start_l
        if start == -1:
            return None
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError:
            return None


def run_mpftp(args: list[str], timeout: float = 60.0) -> StepResult:
    t0 = time.time()
    try:
        proc = subprocess.run(
            [str(MPFTP), *args],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.time() - t0
        return StepResult(
            name=" ".join(args),
            argv=args,
            status="PASS" if proc.returncode == 0 else "FAIL",
            detail="" if proc.returncode == 0 else f"exit {proc.returncode}",
            seconds=elapsed,
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            parsed=parse_json(proc.stdout or ""),
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.time() - t0
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return StepResult(
            name=" ".join(args),
            argv=args,
            status="FAIL",
            detail=f"timeout after {timeout:.0f}s",
            seconds=elapsed,
            stdout=stdout,
            stderr=stderr,
            parsed=parse_json(stdout),
        )


def envelope_error(step: StepResult) -> Optional[str]:
    if isinstance(step.parsed, dict) and step.parsed.get("ok") is False:
        return str(step.parsed.get("error") or "ok: false")
    return None


def expect_ok(step: StepResult) -> Optional[str]:
    err = envelope_error(step)
    if err:
        return err
    if step.returncode not in (0, None) and step.status == "FAIL":
        tail = (step.stderr or step.stdout).strip().splitlines()
        return step.detail or (tail[-1] if tail else "nonzero exit")
    return None


def expect_value(expected: str) -> Check:
    def check(step: StepResult) -> Optional[str]:
        err = expect_ok(step)
        if err:
            return err
        if not isinstance(step.parsed, dict):
            return "no JSON object"
        got = step.parsed.get("value")
        if str(got) != expected:
            return f"value {got!r}, expected {expected!r}"
        return None

    return check


def expect_contains(*needles: str) -> Check:
    def check(step: StepResult) -> Optional[str]:
        err = expect_ok(step)
        if err:
            return err
        blob = (step.stdout + "\n" + json.dumps(step.parsed) if step.parsed is not None else step.stdout)
        for n in needles:
            if n not in blob and n not in (step.stderr or ""):
                return f"missing {n!r}"
        return None

    return check


def expect_error_mentions(*needles: str) -> Check:
    def check(step: StepResult) -> Optional[str]:
        blob = " ".join(
            [
                step.stdout or "",
                step.stderr or "",
                json.dumps(step.parsed) if step.parsed is not None else "",
            ]
        ).lower()
        if step.returncode == 0 and envelope_error(step) is None:
            return "expected a failure"
        for n in needles:
            if n.lower() not in blob:
                return f"error did not mention {n!r}"
        return None

    return check


def ok_or_missing(step: StepResult) -> Optional[str]:
    """rm of a path that was never created is still a successful cleanup."""
    if expect_ok(step) is None:
        return None
    blob = f"{step.stdout}\n{step.stderr}".lower()
    if any(tok in blob for tok in ("no such", "enoent", "not found", "errno 2")):
        return None
    return expect_ok(step)


class Runner:
    def __init__(self, report: Report) -> None:
        self.report = report

    def skip(self, name: str, reason: str) -> StepResult:
        return self.report.add(StepResult(name=name, argv=[], status="SKIP", detail=reason))

    def step(
        self,
        args: list[str],
        *,
        name: Optional[str] = None,
        timeout: float = 60.0,
        check: Check = expect_ok,
        allow_timeout_ok: bool = False,
        watching_ok: bool = False,
    ) -> StepResult:
        result = run_mpftp(args, timeout=timeout)
        result.name = name or result.name
        if allow_timeout_ok and result.detail.startswith("timeout"):
            # Streaming commands (watch / watch-repl) are supposed to run until killed.
            blob = (result.stderr or "") + (result.stdout or "")
            if watching_ok and "watching" not in blob.lower():
                result.status = "FAIL"
                result.detail = "timed out but never printed watching"
            else:
                result.status = "PASS"
                result.detail = "streamed until timeout (expected)"
        else:
            problem = check(result)
            if problem:
                result.status = "FAIL"
                tail = (result.stderr or result.stdout).strip().splitlines()
                last = tail[-1] if tail else ""
                if last and last not in problem:
                    result.detail = f"{problem}: {last[:240]}"
                else:
                    result.detail = problem
            else:
                result.status = "PASS"
                if result.detail.startswith("exit"):
                    result.detail = ""
        return self.report.add(result)


def list_repl_ports() -> list[dict[str, Any]]:
    step = run_mpftp(["ports"], timeout=30)
    if step.returncode != 0 or not isinstance(step.parsed, list):
        raise SystemExit(f"mpftp ports failed: {step.stderr or step.stdout or step.detail}")
    return [p for p in step.parsed if p.get("repl") and p.get("device")]


def write_editor(tmpdir: Path) -> Path:
    path = tmpdir / "editor.py"
    path.write_text(
        "import pathlib, sys\n"
        "p = pathlib.Path(sys.argv[1])\n"
        "p.write_bytes(p.read_bytes() + b'\\nedited-by-cli-test\\n')\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def host_workflows(r: Runner, tmpdir: Path, *, install_latest: bool = False) -> None:
    r.step(["status"], check=lambda s: None if isinstance(s.parsed, dict) else "no JSON")
    r.step(["ports"], check=lambda s: None if isinstance(s.parsed, list) else "expected a port list")
    r.step(["rpc", "ping"], check=lambda s: None if isinstance(s.parsed, dict) and s.parsed.get("pong") else "no pong")

    r.step(["firmware", "discover"], timeout=90)
    r.step(["firmware", "list"], timeout=120)
    r.step(["firmware", "cmods"], timeout=90)
    r.step(
        ["firmware", "artifact", "--port", "esp32", "--board", "ESP32_GENERIC"],
        timeout=60,
        check=lambda s: None
        if s.returncode == 0 or isinstance(s.parsed, dict)
        else "artifact produced no JSON",
    )
    r.step(["firmware", "download-tree"], timeout=120)
    r.step(
        ["firmware", "download-list", "--board", "ESP32_GENERIC"],
        timeout=90,
        check=lambda s: None
        if s.returncode == 0 or isinstance(s.parsed, dict)
        else "download-list produced no JSON",
    )
    r.step(
        ["firmware", "partitions", "get", "--board", "ESP32_GENERIC"],
        timeout=60,
        check=lambda s: None
        if s.returncode == 0 or isinstance(s.parsed, dict)
        else "partitions get produced no JSON",
    )

    r.skip("firmware build", "long host compile; not flashing, not a board workflow")
    r.skip("firmware clean", "would wipe a firmware build tree")
    if install_latest:
        r.skip("firmware download", "run per board with --install-latest-firmware")
        r.skip("firmware flash", "run per board with --install-latest-firmware")
        r.skip("firmware flash --uf2", "used per board when the downloaded image is .uf2")
        r.skip("bootloader", "used per board only when flashing a .uf2")
    else:
        r.skip("firmware download", "fetches a full image; catalog covered by download-tree")
        r.skip("firmware flash", "flashing is out of scope unless --install-latest-firmware")
        r.skip("firmware flash --uf2", "flashing is out of scope unless --install-latest-firmware")
        r.skip("bootloader", "would leave the board in bootloader/UF2 mode")
    r.skip("romfs deploy", "writes a flash partition")

    r.step(
        ["watch", "--from-start"],
        timeout=2.0,
        allow_timeout_ok=True,
        watching_ok=True,
    )

    romdir = tmpdir / "romdir"
    romdir.mkdir()
    (romdir / "hello.txt").write_text("romfs-hello\n", encoding="utf-8")
    rom_out = tmpdir / "cli-test.romfs"
    r.step(
        ["romfs", "build", str(romdir), "-o", str(rom_out)],
        name="romfs build (host)",
        timeout=60,
    )


def _download_returned_artifact(step: StepResult) -> Optional[str]:
    if not isinstance(step.parsed, dict):
        return None
    if step.parsed.get("ok") is False:
        return None
    artifact = step.parsed.get("artifact")
    return str(artifact) if artifact else None


def install_latest_firmware(r: Runner, device: str, detect: StepResult) -> None:
    """Download the latest official image for this board and flash it (destructive)."""
    parsed = detect.parsed if isinstance(detect.parsed, dict) else {}
    match = parsed.get("match") if isinstance(parsed.get("match"), dict) else {}
    board = str(match.get("board") or "").strip()
    variant = str(match.get("variant") or "").strip()
    port = str(match.get("port") or "").strip()
    if not board:
        r.skip(
            f"{device} firmware download latest",
            "detect did not name a board (need an Espressif match)",
        )
        r.skip(f"{device} firmware flash latest", "no download (no board)")
        return

    def download_argv(*, with_variant: bool) -> list[str]:
        args = ["firmware", "download", "--board", board]
        if with_variant and variant:
            args += ["--variant", variant]
        return args

    def download_check(step: StepResult) -> Optional[str]:
        if _download_returned_artifact(step):
            return None
        err = ""
        if isinstance(step.parsed, dict):
            err = str(step.parsed.get("error") or "")
        return err or "download did not return an artifact"

    dl_name = f"{device} firmware download latest"
    if variant:
        dl_name += f" ({board} {variant})"
    else:
        dl_name += f" ({board})"
    dl = r.step(download_argv(with_variant=True), name=dl_name, timeout=180, check=download_check)
    if dl.status != "PASS" and variant:
        dl = r.step(
            download_argv(with_variant=False),
            name=f"{device} firmware download latest ({board}, no variant)",
            timeout=180,
            check=download_check,
        )
    artifact = _download_returned_artifact(dl)
    if dl.status != "PASS" or not artifact:
        r.skip(f"{device} firmware flash latest", "download failed")
        return

    info = dl.parsed if isinstance(dl.parsed, dict) else {}
    flash_port = str(info.get("port") or port or "esp32")
    flash_board = str(info.get("board") or board)
    flash_variant = str(info.get("variant") or "").strip()
    family = str(info.get("family") or "").strip()
    url = str(info.get("url") or artifact)
    is_uf2 = url.lower().endswith(".uf2") or artifact.lower().endswith(".uf2")

    flash = [
        "firmware",
        "flash",
        "--port",
        flash_port,
        "--board",
        flash_board,
        "-d",
        device,
        "--artifact",
        artifact,
    ]
    if flash_variant:
        flash += ["--variant", flash_variant]
    if family:
        flash += ["--family", family]
    if is_uf2:
        r.step(
            ["bootloader", "-d", device],
            name=f"{device} bootloader (UF2)",
            timeout=30,
        )
        time.sleep(4)
        flash.append("--uf2")
        flash_timeout = 90.0
    else:
        flash.append("--erase")
        flash_timeout = 180.0
    flashed = r.step(
        flash,
        name=f"{device} firmware flash latest",
        timeout=flash_timeout,
    )
    if flashed.status != "PASS":
        return
    time.sleep(6)
    after_flash = None
    for attempt in range(1, 6):
        after_flash = run_mpftp(["eval", "-d", device, "1+1"], timeout=45)
        after_flash.name = f"{device} eval after flash"
        problem = expect_value("2")(after_flash)
        if problem is None:
            after_flash.status = "PASS"
            after_flash.detail = "" if attempt == 1 else f"ok on attempt {attempt}"
            r.report.add(after_flash)
            return
        if attempt < 5:
            time.sleep(3)
    assert after_flash is not None
    after_flash.status = "FAIL"
    after_flash.detail = expect_value("2")(after_flash) or "failed after retries"
    r.report.add(after_flash)


def board_workflows(
    r: Runner, device: str, tmpdir: Path, live_rpc: bool, *, install_latest: bool = False
) -> None:
    def b(*parts: str) -> list[str]:
        # Standalone: --device belongs on the subcommand, not before it.
        return [parts[0], "-d", device, *parts[1:]]

    root = BOARD_ROOT
    local_hello = tmpdir / f"{device}_hello.txt"
    local_hello.write_text(f"hello from {device}\n", encoding="utf-8")
    local_got = tmpdir / f"{device}_got.txt"
    local_dir = tmpdir / f"{device}_dir"
    local_dir.mkdir()
    (local_dir / "a.py").write_text("A = 1\n", encoding="utf-8")
    (local_dir / "b.py").write_text("B = 2\n", encoding="utf-8")
    local_mod = tmpdir / "mod.py"
    local_mod.write_text("x = 1\n", encoding="utf-8")
    run_py = tmpdir / "short.py"
    run_py.write_text("print('cli-workflow-run')\n", encoding="utf-8")
    probe_py = tmpdir / "probe.py"
    probe_py.write_text(
        "import os\n"
        f"p = {root!r}\n"
        "try:\n"
        "    os.stat(p)\n"
        "except OSError:\n"
        "    os.mkdir(p)\n"
        "f = open(p + '/result.txt', 'w')\n"
        "f.write('probe-ok')\n"
        "f.close()\n"
        "print('probe-wrote')\n",
        encoding="utf-8",
    )
    loop_py = tmpdir / "loop.py"
    loop_py.write_text(
        "import time\n"
        "while True:\n"
        "    print('loop')\n"
        "    time.sleep(0.25)\n",
        encoding="utf-8",
    )
    editor = write_editor(tmpdir)

    ident = r.step(["connect", device], timeout=45)
    if ident.status != "PASS":
        r.skip(f"{device} remaining board steps", "connect failed")
        return

    interpreter = ""
    if isinstance(ident.parsed, dict):
        interpreter = str(ident.parsed.get("interpreter") or "")
    is_mp = interpreter != "circuitpython"
    is_cp = interpreter == "circuitpython"

    r.step(b("eval", "1+1"), name=f"{device} eval 1+1", check=expect_value("2"))
    r.step(
        b("exec", "print(42)"),
        name=f"{device} exec print(42)",
        check=expect_contains("42"),
    )
    r.step(b("ls", "/"), name=f"{device} ls /")
    r.step(b("ls", "--json", "/"), name=f"{device} ls --json /")
    r.step(b("tree", "/"), name=f"{device} tree /", timeout=90)

    # Isolated workspace on the board.
    r.step(
        b("rm", "-r", root),
        name=f"{device} rm -r {root} (pre-clean)",
        timeout=45,
        check=ok_or_missing,
    )

    r.step(b("mkdir", root), name=f"{device} mkdir")
    r.step(b("touch", f"{root}/empty.txt"), name=f"{device} touch")
    r.step(
        b("put", str(local_hello), f"{root}/hello.txt", "--verify"),
        name=f"{device} put --verify",
    )
    r.step(
        b("get", f"{root}/hello.txt", str(local_got), "--verify"),
        name=f"{device} get --verify",
        check=lambda s: expect_ok(s)
        or (
            None
            if local_got.is_file() and local_got.read_bytes() == local_hello.read_bytes()
            else "downloaded bytes do not match"
        ),
    )
    r.step(b("hash", f"{root}/hello.txt"), name=f"{device} hash")
    r.step(
        b("cp", str(local_hello), f":{root}/copied.txt", "--verify"),
        name=f"{device} cp local→board",
    )
    r.step(
        b("cp", f":{root}/hello.txt", f":{root}/hello2.txt", "--verify"),
        name=f"{device} cp board→board",
    )
    r.step(
        b("cp", str(local_dir), f":{root}/fromcp", "--verify"),
        name=f"{device} cp directory",
    )
    r.step(
        b("put", "-r", str(local_dir), f"{root}/subdir"),
        name=f"{device} put -r directory",
    )
    r.step(
        b("rename", f"{root}/hello2.txt", f"{root}/renamed.txt"),
        name=f"{device} rename",
    )
    r.step(b("tree", root), name=f"{device} tree test dir")
    r.step(b("df"), name=f"{device} df")
    r.step(b("rtc"), name=f"{device} rtc")
    r.step(b("rtc", "--set"), name=f"{device} rtc --set")

    env_editor = os.environ.get("EDITOR")
    os.environ["EDITOR"] = f"{sys.executable} {editor}"
    try:
        r.step(b("edit", f"{root}/empty.txt"), name=f"{device} edit")
    finally:
        if env_editor is None:
            os.environ.pop("EDITOR", None)
        else:
            os.environ["EDITOR"] = env_editor

    if is_mp:
        mpy = run_mpftp(
            b("put", str(local_mod), f"{root}/mod.py", "--mpy", "--verify"),
            timeout=90,
        )
        mpy.name = f"{device} put --mpy"
        blob = f"{mpy.stdout}\n{mpy.stderr}".lower()
        if expect_ok(mpy) is None:
            mpy.status = "PASS"
            mpy.detail = ""
        elif "mpy-cross not found" in blob:
            mpy.status = "SKIP"
            mpy.detail = "mpy-cross not installed on this host (CLI error is as designed)"
        else:
            mpy.status = "FAIL"
            mpy.detail = expect_ok(mpy) or mpy.detail
        r.report.add(mpy)
    else:
        r.skip(f"{device} put --mpy", "MicroPython-only")

    r.step(
        b("run", str(run_py), "--follow"),
        name=f"{device} run --follow",
        check=expect_contains("cli-workflow-run"),
    )
    r.step(
        b("probe", str(probe_py), "--capture", f"{root}/result.txt", "--wait", "2"),
        name=f"{device} probe --capture",
        timeout=90,
        check=lambda s: expect_ok(s)
        or (
            None
            if isinstance(s.parsed, dict)
            and "probe-ok" in str((s.parsed.get("capture") or {}).get("text", ""))
            else "capture did not contain probe-ok"
        ),
    )
    r.step(b("run", str(loop_py)), name=f"{device} run (no-follow loop)")
    r.step(b("interrupt"), name=f"{device} interrupt")

    r.step(
        b("watch-repl"),
        name=f"{device} watch-repl",
        timeout=3.0,
        allow_timeout_ok=True,
        watching_ok=True,
    )

    r.step(b("clean", root, "--dry-run"), name=f"{device} clean --dry-run")
    debris = tmpdir / "probe_scratch.py"
    debris.write_text("# debris\n", encoding="utf-8")
    r.step(
        b("put", str(debris), f"{root}/probe_scratch.py"),
        name=f"{device} put clean-pattern file",
    )
    r.step(b("clean", root), name=f"{device} clean")

    r.step(
        b("rpc", "fs_listdir", json.dumps({"path": root})),
        name=f"{device} rpc fs_listdir",
        check=lambda s: None if isinstance(s.parsed, list) else expect_ok(s) or "expected list",
    )

    if is_mp:
        r.step(
            b("mip", "hmac", "--target", f"{root}/lib", "--no-mpy"),
            name=f"{device} mip hmac",
            timeout=180,
        )
        r.step(
            b("circup", "adafruit_display_text"),
            name=f"{device} circup (expect MP rejection)",
            check=expect_error_mentions("circuitpython"),
        )
        mount_dir = tmpdir / f"{device}_mnt"
        mount_dir.mkdir()
        (mount_dir / "mounted.txt").write_text("from-host\n", encoding="utf-8")
        r.step(b("mount", str(mount_dir)), name=f"{device} mount", timeout=45)
        r.step(b("umount"), name=f"{device} umount", timeout=45)
        r.step(b("romfs", "query"), name=f"{device} romfs query", timeout=45)
    elif is_cp:
        r.step(
            b("mip", "hmac"),
            name=f"{device} mip (expect CP rejection)",
            check=expect_error_mentions("micropython"),
        )
        r.skip(f"{device} circup install", "would mutate /lib; interpreter routing covered by mip rejection")
        r.skip(f"{device} mount", "MicroPython-only")
        r.skip(f"{device} umount", "MicroPython-only")
        r.skip(f"{device} romfs query", "MicroPython-only")
    else:
        r.skip(f"{device} mip/circup/mount/romfs", f"unknown interpreter {interpreter!r}")

    r.step(b("soft-reset"), name=f"{device} soft-reset")
    r.step(b("eval", "1+1"), name=f"{device} eval after soft-reset", check=expect_value("2"))
    r.step(b("soft-reboot"), name=f"{device} soft-reboot")
    time.sleep(2)
    r.step(b("interrupt"), name=f"{device} interrupt after soft-reboot")
    r.step(b("eval", "1+1"), name=f"{device} eval after soft-reboot", check=expect_value("2"))
    r.step(b("hard-reset"), name=f"{device} hard-reset")
    time.sleep(4)
    r.step(
        b("eval", "1+1"),
        name=f"{device} eval after hard-reset",
        timeout=45,
        check=expect_value("2"),
    )

    detect = r.step(
        ["firmware", "detect", "-d", device],
        name=f"{device} firmware detect",
        timeout=90,
        check=lambda s: None
        if s.returncode == 0 or isinstance(s.parsed, dict)
        else "detect produced no JSON",
    )
    time.sleep(5)
    after_detect = None
    for attempt in range(1, 5):
        after_detect = run_mpftp(b("eval", "1+1"), timeout=45)
        after_detect.name = f"{device} eval after detect"
        problem = expect_value("2")(after_detect)
        if problem is None:
            after_detect.status = "PASS"
            after_detect.detail = "" if attempt == 1 else f"ok on attempt {attempt}"
            r.report.add(after_detect)
            break
        if attempt < 4:
            time.sleep(3)
    else:
        after_detect.status = "FAIL"
        after_detect.detail = expect_value("2")(after_detect) or "failed after retries"
        r.report.add(after_detect)

    if live_rpc:
        r.step(["resume"], name=f"{device} resume")
    else:
        r.skip(
            f"{device} resume",
            "standalone sidecar has no last_device; resume needs a live RPC session",
        )

    debug_ports = [
        p
        for p in list_repl_ports()
        if p.get("role") == "cdc_debug" and p.get("device") != device
    ]
    if debug_ports:
        tee = debug_ports[0]["device"]
        r.step(["debug-tee", tee], name=f"{device} debug-tee {tee}", timeout=20)
        r.step(["debug-tee", "--stop"], name=f"{device} debug-tee --stop")
    else:
        r.skip(f"{device} debug-tee", "no cdc_debug port attached")

    r.step(b("rm", "-r", root), name=f"{device} rm -r test dir", timeout=60)
    r.step(["disconnect"], name=f"{device} disconnect")
    if install_latest:
        install_latest_firmware(r, device, detect)


def print_summary(report: Report) -> None:
    passed, failed, skipped = report.counts()
    print()
    print("==== CLI workflow results ====")
    width = max((len(s.name) for s in report.steps), default=8)
    for s in report.steps:
        detail = f"  {s.detail}" if s.detail else ""
        print(f"{s.status:4}  {s.name:<{width}}{detail}")
        if s.status == "FAIL":
            err = (s.stderr or s.stdout).strip()
            if err:
                for line in err.splitlines()[-8:]:
                    print(f"      {line}")
    print()
    print(f"{passed} passed, {failed} failed, {skipped} skipped")
    if failed:
        print("FAILED")
    else:
        print("OK")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--device",
        action="append",
        dest="devices",
        help="Limit to this COM/tty (repeatable). Default: every repl port from mpftp ports",
    )
    parser.add_argument(
        "--install-latest-firmware",
        action="store_true",
        help="Download the latest official release for each board and erase+flash it (wipes the board)",
    )
    args = parser.parse_args(argv)

    if not MPFTP.is_file():
        print(f"missing {MPFTP}", file=sys.stderr)
        return 2

    report = Report()
    runner = Runner(report)
    tmpdir = Path(tempfile.mkdtemp(prefix="mpftp-cli-workflows-"))
    try:
        print(f"host scratch: {tmpdir}", flush=True)
        host_workflows(runner, tmpdir, install_latest=args.install_latest_firmware)

        status = run_mpftp(["status"], timeout=20)
        live_rpc = bool(isinstance(status.parsed, dict) and status.parsed.get("extension_running"))

        ports = list_repl_ports()
        wanted = args.devices
        boards = [p for p in ports if not wanted or p["device"] in wanted]
        if wanted:
            missing = [d for d in wanted if not any(p["device"] == d for p in boards)]
            for d in missing:
                runner.skip(d, "not in mpftp ports (repl)")
        if not boards:
            runner.skip("board workflows", "no REPL ports attached")
        else:
            print(
                "boards: " + ", ".join(f"{p['device']} ({p.get('description') or p.get('hwid')})" for p in boards),
                flush=True,
            )
            for p in boards:
                print(f"\n---- {p['device']} ----", flush=True)
                try:
                    board_workflows(
                        runner,
                        p["device"],
                        tmpdir,
                        live_rpc,
                        install_latest=args.install_latest_firmware,
                    )
                except Exception as exc:
                    runner.report.add(
                        StepResult(
                            name=f"{p['device']} harness",
                            argv=[],
                            status="FAIL",
                            detail=f"{type(exc).__name__}: {exc}",
                        )
                    )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print_summary(report)
    _, failed, _ = report.counts()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
