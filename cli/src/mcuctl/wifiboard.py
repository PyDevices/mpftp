# SPDX-FileCopyrightText: 2026 Brad Barnett
#
# SPDX-License-Identifier: MIT

"""What mcuctl says to a board about Wi-Fi: identity, boot.py, settings.toml, power save.

Pure functions and board-side snippets, so the sidecar stays a caller and the
tests need no board. The decisions behind each piece are in
``docs/guides/connect-over-wifi.md`` and ``docs/how-it-works/wifi-and-bluetooth.md``.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import re
from typing import Any, Optional

#: What a user sees when a board that should answer over WebREPL doesn't.
STUCK_MESSAGE = "The board is busy in a loop that never yields; use serial, or reset it."

#: ``webrepl_setup`` asks for 4 to 9 characters; modwebrepl keeps at most 9.
MIN_PASSWORD_LEN = 4
MAX_PASSWORD_LEN = 9

BOOT_PATH = "/boot.py"

#: MicroPython's webrepl reads its password from this module.
WEBREPL_CFG_PATH = "/webrepl_cfg.py"

#: Where "Enable Wi-Fi access" keeps a webrepl_cfg.py that was already there,
#: so "Disable Wi-Fi access" can put it back.
WEBREPL_CFG_BACKUP_PATH = "/webrepl_cfg.py.mcuctl-orig"

#: CircuitPython reads its Wi-Fi and web workflow settings from here.
SETTINGS_PATH = "/settings.toml"

#: The three settings CircuitPython's web workflow needs, in the order written.
SETTINGS_KEYS = ("CIRCUITPY_WIFI_SSID", "CIRCUITPY_WIFI_PASSWORD", "CIRCUITPY_WEB_API_PASSWORD")

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

BLOCK_BEGIN = "# >>> mcuctl wifi-access >>>"
BLOCK_END = "# <<< mcuctl wifi-access <<<"
_CREATED_NOTE = " (mcuctl created this file)"
_BEGIN_NOTE = ' written by mcuctl; "Disable Wi-Fi access" removes this block'


def check_new_password(password: Optional[str]) -> str:
    """The rule for a password mcuctl writes to a board (upstream's webrepl_setup rule)."""
    if not password or not (MIN_PASSWORD_LEN <= len(password) <= MAX_PASSWORD_LEN):
        raise ValueError(
            f"a WebREPL password is {MIN_PASSWORD_LEN} to {MAX_PASSWORD_LEN} characters "
            "(the board keeps only 9)"
        )
    if not password.isprintable() or not password.isascii():
        raise ValueError("a WebREPL password is plain printable ASCII")
    return password


#: What the MicroPython block runs, as a function so the names it uses don't
#: stay behind in boot.py. It needs only what stock MicroPython has (`network`,
#: `webrepl` and a `secrets.py` that holds WIFI_SSID and WIFI_PASSWORD).
#: `wifi_manager`, when the board has it, joins from the same file and
#: remembers networks; the plain join below is the fallback.
_BLOCK_BODY = """\
def _mcuctl_wifi():
    import network, time, webrepl
    w = network.WLAN(network.STA_IF)
    w.active(True)
    if not w.isconnected():
        try:
            import wifi_manager
            wifi_manager.connect(setup=False)
        except Exception:
            pass
    if not w.isconnected():
        from secrets import WIFI_SSID, WIFI_PASSWORD
        w.connect(WIFI_SSID, WIFI_PASSWORD)
        t = time.ticks_ms()
        while not w.isconnected() and time.ticks_diff(time.ticks_ms(), t) < 20000:
            time.sleep_ms(100)
    if w.isconnected():
        webrepl.start()
    else:
        print("mcuctl wifi-access: no Wi-Fi after 20 s; reset to try again")
try:
    _mcuctl_wifi()
except Exception as e:
    print("mcuctl wifi-access:", e)
del _mcuctl_wifi
"""


def render_block(*, created: bool = False) -> str:
    """The lines "Enable Wi-Fi access" puts at the top of boot.py.

    The password is not in it: webrepl reads it from /webrepl_cfg.py.
    """
    begin = BLOCK_BEGIN + (_CREATED_NOTE if created else _BEGIN_NOTE)
    return f"{begin}\n{_BLOCK_BODY}{BLOCK_END}\n"


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


def add_block(content: Optional[str]) -> str:
    """boot.py with the block on top. ``content`` None means there is no boot.py.

    It goes first so a failure further down boot.py can't cost you Wi-Fi
    access, and so removing it gives back the original bytes exactly.
    """
    if content is not None and find_block(content):
        raise ValueError("boot.py already has mcuctl's Wi-Fi block; disable it first")
    return render_block(created=content is None) + (content or "")


def remove_block(content: str) -> Optional[str]:
    """boot.py without the block, or None when mcuctl created the file (delete it)."""
    span = find_block(content)
    if span is None:
        raise ValueError("boot.py has no mcuctl Wi-Fi block")
    start, end = span
    first_line = content[start : content.find("\n", start)]
    rest = content[:start] + content[end:]
    if _CREATED_NOTE in first_line and not rest:
        return None
    return rest


def webrepl_cfg_text(password: str) -> str:
    """The /webrepl_cfg.py "Enable Wi-Fi access" writes: ``PASS = "..."``, the
    line upstream's ``webrepl_setup`` writes, under a note saying who wrote it."""
    check_new_password(password)
    quoted = password.replace("\\", "\\\\").replace('"', '\\"')
    return f"{CFG_NOTE}\nPASS = \"{quoted}\"\n"


#: First line of a webrepl_cfg.py mcuctl wrote, so disable knows it is ours.
CFG_NOTE = '# written by mcuctl; "Disable Wi-Fi access" removes this file'


def cfg_is_ours(content: Optional[str]) -> bool:
    return bool(content) and content.startswith(CFG_NOTE)


_MASKED = "*****"
_NEW = "(new)"
_SECRET_LINE = re.compile(
    r"^(\s*(?:PASS|%s)\s*=\s*)(.*?)(\r?\n?)$" % "|".join(SETTINGS_KEYS),
    re.MULTILINE,
)
_START_PASSWORD = re.compile(r"(webrepl\.start\(password=)[^)]*\)")


def mask_secrets(text: str, password: Optional[str] = None) -> str:
    """Hide every password and Wi-Fi name before text is shown or logged. What
    an existing file holds shows as ``*****``; the password being written, when
    known, shows as ``(new)``, so a diff still shows that the line changed."""
    if password:
        text = text.replace(repr(password), repr(_NEW)).replace(f'"{password}"', f'"{_NEW}"')
    text = _START_PASSWORD.sub(lambda m: m.group(1) + repr(_MASKED) + ")", text)

    def hide(m):
        value = m.group(2) if m.group(2) == f'"{_NEW}"' else f'"{_MASKED}"'
        return m.group(1) + value + m.group(3)

    return _SECRET_LINE.sub(hide, text)


def unified_diff(
    old: Optional[str], new: Optional[str], path: str = BOOT_PATH, context: int = 3
) -> str:
    """A unified diff for the confirmation prompt. None means "no file"."""
    lines = difflib.unified_diff(
        (old or "").splitlines(keepends=True),
        (new or "").splitlines(keepends=True),
        fromfile=path if old is not None else "/dev/null",
        tofile=path if new is not None else "/dev/null",
        n=context,
    )
    text = "".join(line if line.endswith("\n") else line + "\n" for line in lines)
    return text


def sha256_text(content: Optional[str]) -> Optional[str]:
    if content is None:
        return None
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


#: What "Enable Wi-Fi access" needs on a MicroPython board, checked without
#: printing anything from secrets.py: webrepl, a secrets module somewhere on
#: sys.path, and the two names the block reads from it.
PREREQ_CODE = """\
_r={}
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
_r['names']=False
if _r['secrets']:
 try:
  import secrets as _s
  _r['names']=hasattr(_s,'WIFI_SSID') and hasattr(_s,'WIFI_PASSWORD')
 except Exception:
  pass
print(repr(_r))
del _r
"""


def prereq_problems(prereqs: dict[str, Any]) -> list[str]:
    """Plain sentences for whatever a MicroPython board is missing."""
    problems = []
    if not prereqs.get("webrepl"):
        problems.append("This firmware has no webrepl module, so it can't be reached over Wi-Fi.")
    if not prereqs.get("secrets"):
        problems.append(
            "The board has no secrets.py holding WIFI_SSID and WIFI_PASSWORD, so it "
            "can't join your network. Copy one on with mcuctl put (the board reads it; "
            "mcuctl never does)."
        )
    elif not prereqs.get("names"):
        problems.append("The board's secrets.py doesn't define WIFI_SSID and WIFI_PASSWORD.")
    return problems


# --------------------------------------------------------------------------
# CircuitPython: the settings.toml block
# --------------------------------------------------------------------------

#: What "Enable Wi-Fi access" checks on a CircuitPython board, without printing
#: anything from secrets.py: whether it has Wi-Fi credentials to copy, in the
#: form this stack uses (WIFI_SSID, WIFI_PASSWORD) or Adafruit's old
#: ``secrets = {"ssid": ..., "password": ...}``.
CP_PREREQ_CODE = """\
_r={'secrets':False}
try:
 import secrets as _s
 _d=getattr(_s,'secrets',None)
 if not isinstance(_d,dict):
  _d={}
 _r['secrets']=bool((getattr(_s,'WIFI_SSID',None) or _d.get('ssid')) and (getattr(_s,'WIFI_PASSWORD',None) is not None or _d.get('password') is not None))
except Exception:
 pass
print(repr(_r))
del _r
"""

#: Prints True when the board can't write its own drive (the computer has it mounted).
CP_READONLY_CODE = "import storage\nprint(storage.getmount('/').readonly)\n"

_KEY_LINE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*=", re.MULTILINE)


def settings_keys_outside_block(content: Optional[str]) -> set[str]:
    """Which of the three Wi-Fi settings the file already defines itself."""
    text = content or ""
    span = find_block(text)
    if span:
        text = text[: span[0]] + text[span[1] :]
    return {k for k in _KEY_LINE.findall(text) if k in SETTINGS_KEYS}


def settings_block_keys(content: Optional[str]) -> list[str]:
    """The settings mcuctl's block would add: the ones the file doesn't define."""
    have = settings_keys_outside_block(content)
    return [k for k in SETTINGS_KEYS if k not in have]


def render_settings_block(keys: list[str], *, created: bool = False, values: Optional[dict] = None) -> str:
    """The lines "Enable Wi-Fi access" puts at the top of settings.toml. Without
    ``values`` they are the masked preview shown for confirmation."""
    begin = BLOCK_BEGIN + (_CREATED_NOTE if created else _BEGIN_NOTE)
    lines = [begin]
    for key in keys:
        value = (values or {}).get(key, _MASKED)
        escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{key} = "{escaped}"')
    lines.append(BLOCK_END)
    return "\n".join(lines) + "\n"


#: Runs on the CircuitPython board: builds the settings.toml block there, from
#: its own secrets.py, and puts it at the top of the file. The Wi-Fi name and
#: password never leave the board; it prints only an ``MCUCTL-`` status line.
#: ``{path}``, ``{begin}``, ``{end}``, ``{keys}``, ``{webpw}`` are filled in by
#: ``cp_enable_code``.
CP_ENABLE_CODE = """\
def _mcuctl_cp(path, begin, note_new, note_old, end, keys, webpw):
 try:
  f=open(path)
  old=f.read()
  f.close()
  created=False
 except OSError:
  old=''
  created=True
 v={{'CIRCUITPY_WEB_API_PASSWORD':webpw}}
 if 'CIRCUITPY_WIFI_SSID' in keys or 'CIRCUITPY_WIFI_PASSWORD' in keys:
  try:
   import secrets as s
  except ImportError:
   return 'MCUCTL-ERR no secrets.py'
  d=getattr(s,'secrets',None)
  if not isinstance(d,dict):
   d={{}}
  ssid=getattr(s,'WIFI_SSID',None) or d.get('ssid')
  pw=getattr(s,'WIFI_PASSWORD',None)
  if pw is None:
   pw=d.get('password')
  if not ssid or pw is None:
   return 'MCUCTL-ERR secrets.py has no Wi-Fi name and password'
  v['CIRCUITPY_WIFI_SSID']=ssid
  v['CIRCUITPY_WIFI_PASSWORD']=pw
 out=begin+(note_new if created else note_old)+'\\n'
 for k in keys:
  out+=k+' = "'+str(v[k]).replace('\\\\','\\\\\\\\').replace('"','\\\\"')+'"\\n'
 out+=end+'\\n'
 try:
  f=open(path,'w')
  f.write(out+old)
  f.close()
 except OSError as e:
  return 'MCUCTL-ERR the board can not write '+path+' (%s)'%e
 return 'MCUCTL-OK'
print(_mcuctl_cp({path!r},{begin!r},{note_new!r},{note_old!r},{end!r},{keys!r},{webpw!r}))
del _mcuctl_cp
"""


def cp_enable_code(keys: list[str], web_password: str, *, path: str = SETTINGS_PATH) -> str:
    return CP_ENABLE_CODE.format(
        path=path, begin=BLOCK_BEGIN, note_new=_CREATED_NOTE, note_old=_BEGIN_NOTE, end=BLOCK_END, keys=list(keys), webpw=web_password
    )


def parse_cp_enable(text: Any) -> Optional[str]:
    """None when the board wrote the block, else the reason it didn't."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", "replace")
    for line in str(text or "").splitlines():
        if line.startswith("MCUCTL-OK"):
            return None
        if line.startswith("MCUCTL-ERR "):
            return line[len("MCUCTL-ERR ") :].strip()
    return "the board didn't answer"


#: Runs the block now, so no reset is needed, then prints the address. The
#: sidecar keeps only the MCUCTL-IP line: the helper's own messages name the
#: network, and they stay out of every reply and log.
START_NOW_CODE = """\
exec(open('boot.py').read().split({end!r})[0])
import network
_w=network.WLAN(network.STA_IF)
print('MCUCTL-IP', _w.ifconfig()[0] if _w.isconnected() else '-')
del _w
"""


def parse_start_now(text: Any) -> Optional[str]:
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", "replace")
    for line in str(text or "").splitlines():
        if line.startswith("MCUCTL-IP "):
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
