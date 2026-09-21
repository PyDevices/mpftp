"""No-UAC recovery for an ESP32 wedged on its own USB (mpftp#31).

An ESP32-S3 whose only serial line is its native USB -- the LilyGO T-Embed --
cannot be reset by the host: DTR and RTS go nowhere, so `mpftp bootloader` asks
the *firmware* to delete its USB PHY and reboot. Windows is often left serving
the node the chip no longer presents, and every open fails until that node is
re-enumerated. Re-enumerating it needs elevation, which an unattended session
has nobody to grant.

The way through is a scheduled task, ``mpftp-restart-esp-usb``, registered once
with elevation and started on demand by any account. This module is the
unprivileged half: it finds the instance id, leaves it in the request file the
task's script reads, starts the task, and reports what came back.

**Check before you plan around it.** The task registered on a machine may not
be the task this repo ships. The first one on this bench ran
``Get-PnpDevice | Restart-PnpDevice``, and there is no ``Restart-PnpDevice`` in
Windows PowerShell -- so it failed on every device, reported LastTaskResult 1,
and was indistinguishable from a board that refused to come back. That cost a
night's unattended flashing. :func:`task_state` exists so a caller can tell
"the recovery is not installed" from "the board did not make it", and
:func:`recovery_available` is the one-line question to ask first.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

TASK_NAME = "mpftp-restart-esp-usb"

#: The one shape a request may take: the composite parent of an Espressif
#: (VID_303A) USB device. Anchored at both ends, so a trailing ``; calc`` or a
#: second path component cannot ride along, and the ``\\`` after the product id
#: rejects ``&MI_00`` children -- restarting the parent brings its interfaces
#: with it. The task's PowerShell side enforces this same rule independently;
#: this copy is here so a bad id is refused before it is ever written down.
INSTANCE_ID_RE = re.compile(r"^USB\\VID_303A&PID_[0-9A-Fa-f]{4}\\[0-9A-Za-z&_.\-]+$")

MAX_REQUEST_CHARS = 200
_TASK_WAIT_SECS = 45.0


class RecoveryError(RuntimeError):
    """The recovery could not be driven -- not the same as the board failing."""


# --------------------------------------------------------------- powershell

def _powershell() -> str:
    exe = shutil.which("powershell.exe") or shutil.which("powershell")
    if not exe:
        raise RecoveryError(
            "powershell.exe is not on PATH. This recovery is Windows-only; from WSL it "
            "reaches Windows through interop, which needs /mnt/c/.../powershell.exe visible."
        )
    return exe


def _ps(script: str, timeout: float = 60.0) -> str:
    """Run a fixed PowerShell snippet. Never interpolate caller data in here."""
    proc = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RecoveryError(f"powershell failed: {proc.stderr.strip() or proc.returncode}")
    return proc.stdout


def _win_to_local(win_path: str) -> Path:
    """Translate a Windows path to whatever this interpreter can open."""
    win_path = win_path.strip()
    if sys.platform == "win32":
        return Path(win_path)
    wslpath = shutil.which("wslpath")
    if not wslpath:
        raise RecoveryError(f"cannot translate {win_path!r} without wslpath")
    proc = subprocess.run([wslpath, "-u", win_path], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RecoveryError(f"wslpath could not translate {win_path!r}: {proc.stderr.strip()}")
    return Path(proc.stdout.strip())


def _known_folder(var: str) -> str:
    out = _ps(f"[Console]::Out.Write($env:{var})").strip()
    if not out:
        raise RecoveryError(f"Windows did not report %{var}%")
    return out


def request_path() -> Path:
    """Where an unprivileged caller leaves the instance id. Data, never code."""
    return _win_to_local(_known_folder("ProgramData")) / "mpftp" / "restart-esp-usb.target"


def log_path() -> Path:
    """The task's transcript. Written by SYSTEM, readable here, not writable here."""
    return _win_to_local(_known_folder("ProgramFiles")) / "mpftp" / "restart-esp-usb.log"


# ------------------------------------------------------------------ devices

def espressif_devices() -> list[dict[str, str]]:
    """Every attached VID_303A composite parent, so ids are found and not guessed.

    ``&MI_`` children are left out: they are what you must *not* restart.
    """
    out = _ps(
        "Get-PnpDevice -PresentOnly "
        "| Where-Object { $_.InstanceId -like 'USB\\VID_303A*' -and $_.InstanceId -notmatch '&MI_' } "
        "| ForEach-Object { $_.InstanceId + '|' + $_.Status + '|' + $_.FriendlyName }"
    )
    devices = []
    for line in out.splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) == 3 and parts[0]:
            devices.append({"instanceId": parts[0], "status": parts[1], "name": parts[2]})
    return devices


def validate_instance_id(instance_id: str) -> str:
    """Return *instance_id* if it is one we will ever write down, else raise."""
    if not isinstance(instance_id, str) or not instance_id.strip():
        raise ValueError("no instance id given")
    candidate = instance_id.strip()
    if len(candidate) > MAX_REQUEST_CHARS:
        raise ValueError(f"instance id is {len(candidate)} chars, limit is {MAX_REQUEST_CHARS}")
    if not INSTANCE_ID_RE.match(candidate):
        raise ValueError(
            f"{instance_id!r} is not an Espressif USB instance id. Expected the composite "
            f"parent of a VID_303A device, e.g. 'USB\\VID_303A&PID_4003\\3485186BFCAC0000' -- "
            f"not an &MI_ child, not another vendor, nothing appended. "
            f"`mpftp usb-restart --list` prints the attached ones."
        )
    return candidate


# --------------------------------------------------------------- the task

def task_state() -> dict[str, Any]:
    """What the registered task would actually do, and how it last went."""
    out = _ps(
        f"$t = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue; "
        "if (-not $t) { 'installed|no' } else { "
        "  'installed|yes'; "
        "  'execute|'   + $t.Actions[0].Execute; "
        "  'arguments|' + ($t.Actions[0].Arguments -replace '[\\r\\n]+', ' '); "
        "  'user|'      + $t.Principal.UserId; "
        "  'runlevel|'  + $t.Principal.RunLevel; "
        f"  $i = Get-ScheduledTaskInfo -TaskName '{TASK_NAME}'; "
        "  'lastResult|' + $i.LastTaskResult; "
        "  'lastRun|'    + $i.LastRunTime; "
        "  'state|'      + $t.State }"
    )
    fields: dict[str, str] = {}
    for line in out.splitlines():
        key, _, value = line.strip().partition("|")
        if key:
            fields[key] = value

    state: dict[str, Any] = {
        "taskName": TASK_NAME,
        "installed": fields.get("installed") == "yes",
        "action": None,
        "runsAs": fields.get("user"),
        "runLevel": fields.get("runlevel"),
        "lastResult": None,
        "lastRun": fields.get("lastRun") or None,
        "usable": False,
        "reason": "",
    }
    if fields.get("lastResult", "").strip().lstrip("-").isdigit():
        state["lastResult"] = int(fields["lastResult"])

    if not state["installed"]:
        state["reason"] = (
            f"no scheduled task named {TASK_NAME}. Nothing can recover a wedged ESP32 USB "
            f"node without elevation until an administrator runs "
            f"tools/windows/install-restart-esp-usb-task.ps1."
        )
        return state

    arguments = fields.get("arguments", "")
    state["action"] = f"{fields.get('execute', '')} {arguments}".strip()

    if "Restart-PnpDevice" in arguments:
        state["reason"] = (
            "the registered task calls Restart-PnpDevice, which does not exist in Windows "
            "PowerShell's PnpDevice module -- it fails on every device and reports "
            "LastTaskResult 1, which looks exactly like a board that refused to come back "
            "(mpftp#31). Re-register it with tools/windows/install-restart-esp-usb-task.ps1 "
            "from an administrator PowerShell."
        )
    elif "restart-esp-usb.ps1" not in arguments:
        state["reason"] = (
            "the registered task does not run restart-esp-usb.ps1, so mpftp cannot say what "
            f"it would do. Its action is: {state['action']}"
        )
    elif (state["runsAs"] or "").upper() not in ("SYSTEM", "NT AUTHORITY\\SYSTEM"):
        state["reason"] = (
            f"the task runs as {state['runsAs']}, not SYSTEM, so pnputil /restart-device "
            f"inside it will fail with Access is denied."
        )
    else:
        state["usable"] = True
        state["reason"] = "ready"
    return state


def recovery_available() -> tuple[bool, str]:
    """Ask before planning around it: is the no-prompt recovery really there?"""
    try:
        state = task_state()
    except RecoveryError as exc:
        return False, str(exc)
    return bool(state["usable"]), state["reason"]


def _task_is_running() -> bool:
    out = _ps(f"(Get-ScheduledTask -TaskName '{TASK_NAME}').State")
    return out.strip().lower() == "running"


def restart_device(instance_id: str, wait: float = _TASK_WAIT_SECS) -> dict[str, Any]:
    """Restart one Espressif USB node through the task. No elevation here.

    Returns the task's exit code and the tail of its transcript. The exit codes
    are the script's: 0 restarted, 2 no request, 3 request refused, 4 no such
    device attached, 5 the restart itself failed.
    """
    target = validate_instance_id(instance_id)

    available, reason = recovery_available()
    if not available:
        raise RecoveryError(reason)

    request = request_path()
    try:
        request.parent.mkdir(parents=True, exist_ok=True)
        # The id goes into a file and nowhere else. It is never spliced into a
        # command line, here or on the PowerShell side.
        request.write_text(target + "\n", encoding="utf-8")
    except OSError as exc:
        raise RecoveryError(f"could not write the request file {request}: {exc}") from exc

    before = task_state().get("lastRun")
    _ps(f"Start-ScheduledTask -TaskName '{TASK_NAME}'")

    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if not _task_is_running():
            break
        time.sleep(0.5)
    else:
        raise RecoveryError(
            f"{TASK_NAME} was still running after {wait:.0f}s. Read its transcript at {log_path()}."
        )

    after = task_state()
    result = {
        "instanceId": target,
        "taskResult": after.get("lastResult"),
        "lastRun": after.get("lastRun"),
        "ranThisTime": after.get("lastRun") != before,
        "log": tail_log(),
    }
    result["ok"] = result["taskResult"] == 0
    if not result["ok"]:
        result["hint"] = _EXIT_HINTS.get(
            result["taskResult"],
            "see the transcript; a non-zero result is the script's own exit code.",
        )
    return result


_EXIT_HINTS = {
    1: "PowerShell itself failed before the script ran -- check the task's action.",
    2: "the task found no request file. Something consumed it first, or the write did not land.",
    3: "the request was refused as malformed. mpftp validates the same rule before writing, "
       "so this means the file was changed between the two.",
    4: "no device with that instance id is attached. It may already have re-enumerated -- "
       "look for 303A:1001 on a new COM number.",
    5: "the restart itself failed. The transcript has pnputil's own words.",
}


def tail_log(lines: int = 20) -> list[str]:
    try:
        path = log_path()
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except (OSError, RecoveryError):
        return []
