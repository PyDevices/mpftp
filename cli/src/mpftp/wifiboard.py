"""What mpftp says to a board about Wi-Fi: identity, boot.py, power save.

Pure functions and board-side snippets, so the sidecar stays a caller and the
tests need no board. The decisions behind each piece are in
``docs/plans/wifi-webrepl.md``.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
from typing import Any, Optional

#: What a user sees when a board that should answer over WebREPL doesn't.
STUCK_MESSAGE = "The board is busy in a loop that never yields; use serial, or reset it."

#: ``webrepl_setup`` asks for 4 to 9 characters; modwebrepl keeps at most 9.
MIN_PASSWORD_LEN = 4
MAX_PASSWORD_LEN = 9

BOOT_PATH = "/boot.py"

# --------------------------------------------------------------------------
# Identity: which board is this, and is its Wi-Fi up?
# --------------------------------------------------------------------------

#: One exec over the raw REPL. Prints a single ``repr(dict)`` line. Reads no
#: file, so ``secrets.py`` is never touched.
IDENTITY_CODE = """\
_r={}
try:
 import binascii,machine
 _r['uid']=binascii.hexlify(machine.unique_id()).decode()
except Exception:
 pass
try:
 import sys
 _r['machine']=sys.implementation._machine
except Exception:
 pass
try:
 import network
 try:
  _r['hostname']=network.hostname()
 except Exception:
  pass
 _w=network.WLAN(network.STA_IF)
 if _w.active() and _w.isconnected():
  _r['ip']=_w.ifconfig()[0]
 del _w
except Exception:
 pass
try:
 import webrepl
 _r['webrepl']=bool(getattr(webrepl,'listen_s',None))
except Exception:
 _r['webrepl']=False
print(repr(_r))
del _r
"""


def parse_identity(text: Any) -> Optional[dict[str, Any]]:
    """The dict IDENTITY_CODE printed, or None if the output isn't one."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", "replace")
    for line in reversed(str(text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = ast.literal_eval(line)
            except (ValueError, SyntaxError):
                return None
            if not isinstance(value, dict):
                return None
            if value.get("ip") in ("0.0.0.0", ""):
                value.pop("ip")
            return value
    return None


def friendly_name(identity: dict[str, Any]) -> str:
    """A short label: the board's network hostname, else its uid."""
    return str(identity.get("hostname") or identity.get("uid") or "board")


# --------------------------------------------------------------------------
# boot.py: the "Enable Wi-Fi access" block
# --------------------------------------------------------------------------

BLOCK_BEGIN = "# >>> mpftp wifi-access >>>"
BLOCK_END = "# <<< mpftp wifi-access <<<"
_CREATED_NOTE = " (mpftp created this file)"
_BEGIN_NOTE = ' written by mpftp; "Disable Wi-Fi access" removes this block'


def check_new_password(password: Optional[str]) -> str:
    """The rule for a password mpftp writes to a board (upstream's webrepl_setup rule)."""
    if not password or not (MIN_PASSWORD_LEN <= len(password) <= MAX_PASSWORD_LEN):
        raise ValueError(
            f"a WebREPL password is {MIN_PASSWORD_LEN} to {MAX_PASSWORD_LEN} characters "
            "(the board keeps only 9)"
        )
    if not password.isprintable() or not password.isascii():
        raise ValueError("a WebREPL password is plain printable ASCII")
    return password


def render_block(password: str, *, created: bool = False) -> str:
    """The lines "Enable Wi-Fi access" puts at the top of boot.py."""
    check_new_password(password)
    begin = BLOCK_BEGIN + (_CREATED_NOTE if created else _BEGIN_NOTE)
    return (
        f"{begin}\n"
        "try:\n"
        "    import webrepl, wifi\n"
        "    wifi.connect_from_secrets()\n"
        f"    webrepl.start(password={password!r})\n"
        "except Exception as e:\n"
        '    print("mpftp wifi-access:", e)\n'
        f"{BLOCK_END}\n"
    )


def find_block(content: str) -> Optional[tuple[int, int]]:
    """``(start, end)`` of the block, markers and final newline included."""
    start = content.find(BLOCK_BEGIN)
    if start < 0 or (start > 0 and content[start - 1] != "\n"):
        return None
    end_at = content.find(BLOCK_END, start)
    if end_at < 0:
        return None
    end = end_at + len(BLOCK_END)
    if content[end : end + 2] == "\r\n":
        end += 2
    elif content[end : end + 1] == "\n":
        end += 1
    return start, end


def add_block(content: Optional[str], password: str) -> str:
    """boot.py with the block on top. ``content`` None means there is no boot.py.

    It goes first so a failure further down boot.py can't cost you Wi-Fi
    access, and so removing it gives back the original bytes exactly.
    """
    if content is not None and find_block(content):
        raise ValueError("boot.py already has mpftp's Wi-Fi block; disable it first")
    return render_block(password, created=content is None) + (content or "")


def remove_block(content: str) -> Optional[str]:
    """boot.py without the block, or None when mpftp created the file (delete it)."""
    span = find_block(content)
    if span is None:
        raise ValueError("boot.py has no mpftp Wi-Fi block")
    start, end = span
    first_line = content[start : content.find("\n", start)]
    rest = content[:start] + content[end:]
    if _CREATED_NOTE in first_line and not rest:
        return None
    return rest


def mask_password(text: str, password: Optional[str]) -> str:
    """Hide a password inside the block before anything is shown or logged."""
    if not password:
        return text
    return text.replace(f"password={password!r}", "password='" + "*" * len(password) + "'")


def mask_block_passwords(text: str) -> str:
    """Hide whatever password an existing block holds (we don't know it)."""
    out = []
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("webrepl.start(password="):
            indent = line[: len(line) - len(stripped)]
            nl = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
            line = f"{indent}webrepl.start(password='*****'){nl}"
        out.append(line)
    return "".join(out)


def unified_diff(old: Optional[str], new: Optional[str], path: str = BOOT_PATH) -> str:
    """A unified diff for the confirmation prompt. None means "no file"."""
    lines = difflib.unified_diff(
        (old or "").splitlines(keepends=True),
        (new or "").splitlines(keepends=True),
        fromfile=path if old is not None else "/dev/null",
        tofile=path if new is not None else "/dev/null",
    )
    text = "".join(line if line.endswith("\n") else line + "\n" for line in lines)
    return text


def sha256_text(content: Optional[str]) -> Optional[str]:
    if content is None:
        return None
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


#: What "Enable Wi-Fi access" needs on the board, checked without reading
#: secrets.py: the wifi helper, a secrets module somewhere on sys.path, webrepl.
PREREQ_CODE = """\
_r={}
try:
 import wifi
 _r['wifi']=hasattr(wifi,'connect_from_secrets')
except Exception:
 _r['wifi']=False
try:
 import webrepl
 _r['webrepl']=True
except Exception:
 _r['webrepl']=False
import os,sys
_r['secrets']=False
for _d in sys.path:
 for _n in ('secrets.py','secrets.mpy'):
  try:
   os.stat((_d.rstrip('/') or '')+'/'+_n)
   _r['secrets']=True
  except OSError:
   pass
try:
 os.stat('boot.py')
 _r['boot']=True
except OSError:
 _r['boot']=False
print(repr(_r))
del _r
"""


def prereq_problems(prereqs: dict[str, Any]) -> list[str]:
    """Plain sentences for whatever the board is missing."""
    problems = []
    if not prereqs.get("webrepl"):
        problems.append("This firmware has no webrepl module, so it can't be reached over Wi-Fi.")
    if not prereqs.get("wifi"):
        problems.append(
            "The board has no wifi helper with connect_from_secrets(). Install it with "
            "mip.install('pydevices') (see pydevices' board-bringup guide)."
        )
    if not prereqs.get("secrets"):
        problems.append(
            "The board has no secrets.py holding WIFI_SSID and WIFI_PASSWORD, so it "
            "can't join your network. Put one in /lib first."
        )
    return problems


#: Runs the block now, so no reset is needed, then prints the address. The
#: sidecar keeps only the MPFTP-IP line: the helper's own messages name the
#: network, and they stay out of every reply and log.
START_NOW_CODE = """\
exec(open('boot.py').read().split({end!r})[0])
import network
_w=network.WLAN(network.STA_IF)
print('MPFTP-IP', _w.ifconfig()[0] if _w.isconnected() else '-')
del _w
"""


def parse_start_now(text: Any) -> Optional[str]:
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", "replace")
    for line in str(text or "").splitlines():
        if line.startswith("MPFTP-IP "):
            ip = line.split(None, 1)[1].strip()
            return None if ip in ("-", "0.0.0.0") else ip
    return None


# --------------------------------------------------------------------------
# Power save during transfers
# --------------------------------------------------------------------------

#: Prints ``(pm, PM_NONE, ble_active)``. Bluetooth being active means leave pm
#: alone: ESP-IDF needs modem sleep for Wi-Fi/BLE coexistence.
PM_READ_CODE = """\
import network
_w=network.WLAN(network.STA_IF)
try:
 import bluetooth
 _b=bool(bluetooth.BLE().active())
except Exception:
 _b=False
print(repr((_w.config('pm'),getattr(_w,'PM_NONE',0),_b)))
del _w,_b
"""


def pm_set_code(value: int) -> str:
    return f"import network\nnetwork.WLAN(network.STA_IF).config(pm={int(value)})\n"


def parse_pm(text: Any) -> Optional[tuple[int, int, bool]]:
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", "replace")
    for line in reversed(str(text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("("):
            try:
                pm, none, ble = ast.literal_eval(line)
                return int(pm), int(none), bool(ble)
            except (ValueError, SyntaxError, TypeError):
                return None
    return None
