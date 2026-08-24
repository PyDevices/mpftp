"""``~/.mpftp/config.json`` — one settings file for every mpftp frontend.

JSON rather than TOML so the file looks like every other tool config a coding
agent already knows how to read and edit. (CircuitPython's on-device
``/settings.toml`` is defined by CircuitPython and is unrelated to this.)

Precedence, highest first:

1. An explicit value passed to the operation (CLI flag, RPC parameter, UI field)
2. VS Code workspace settings handed to the engine by the extension
3. Environment variables
4. ``~/.mpftp/config.json``
5. The built-in default

Layers 1 and 2 belong to the caller: pass what you were given to
:func:`resolve` as ``override``, and this module supplies the rest.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

CONFIG_DIR = Path.home() / ".mpftp"
CONFIG_PATH = CONFIG_DIR / "config.json"

#: name -> (type, default, environment variable or None)
SETTINGS: dict[str, tuple[type, Any, Optional[str]]] = {
    # Board session
    "pythonPath": (str, "", "MPFTP_PYTHON"),
    "mpremotePath": (str, "", None),
    "defaultBaud": (int, 115200, "MPFTP_BAUD"),
    "autoConnectDevice": (str, "", "MPFTP_DEVICE"),
    "verifyTransfers": (bool, True, None),
    "compileOnUpload": (bool, False, None),
    "autoReconnectAfterReset": (bool, True, None),
    "openEditorOnConnect": (bool, True, None),
    # Firmware workspace and toolchains
    "micropythonPath": (str, "", "MPFTP_MICROPYTHON"),
    "workspacePath": (str, "", "MPFTP_WORKSPACE"),
    "idfPath": (str, "", "IDF_PATH"),
    "emsdkPath": (str, "", "EMSDK"),
    "toolchainBins": (list, [], None),
    "buildPythonPath": (str, "", "MPFTP_BUILD_PYTHON"),
    "esptoolCommand": (str, "", "MPFTP_ESPTOOL"),
}

#: Firmware panel state (last selection, last device, flash preferences). Free
#: form: the panel owns its shape, this module only persists it.
FIRMWARE_KEY = "firmware"


class ConfigError(ValueError):
    """The configuration file is unusable, or a value has the wrong type."""


def _coerce(name: str, value: Any, want: type) -> Any:
    if want is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("1", "true", "yes", "on"):
                return True
            if lowered in ("0", "false", "no", "off"):
                return False
        raise ConfigError(f"{name}: expected a boolean, got {value!r}")
    if want is int:
        if isinstance(value, bool):
            raise ConfigError(f"{name}: expected an integer, got {value!r}")
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{name}: expected an integer, got {value!r}") from None
    if want is list:
        if isinstance(value, list):
            return [str(v) for v in value]
        if isinstance(value, str):
            return [part for part in value.split(os.pathsep) if part]
        raise ConfigError(f"{name}: expected a list, got {value!r}")
    return str(value)


def read_file() -> dict[str, Any]:
    """Parse the config file. A missing file is empty; a corrupt one is an error."""
    try:
        raw = CONFIG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise ConfigError(f"cannot read {CONFIG_PATH}: {e}") from e

    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{CONFIG_PATH} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{CONFIG_PATH} must contain a JSON object")
    return data


def validate(data: dict[str, Any]) -> dict[str, Any]:
    """Type-check known settings. Unknown keys are reported, not silently kept."""
    checked: dict[str, Any] = {}
    unknown = []
    for name, value in data.items():
        if name == FIRMWARE_KEY:
            if not isinstance(value, dict):
                raise ConfigError(f"{FIRMWARE_KEY}: expected an object, got {value!r}")
            checked[name] = value
            continue
        spec = SETTINGS.get(name)
        if spec is None:
            unknown.append(name)
            continue
        checked[name] = _coerce(name, value, spec[0])
    if unknown:
        raise ConfigError(f"unknown setting(s) in {CONFIG_PATH}: {', '.join(sorted(unknown))}")
    return checked


def load() -> dict[str, Any]:
    """Every setting, with file and environment applied over the defaults."""
    values = {name: spec[1] for name, spec in SETTINGS.items()}
    values[FIRMWARE_KEY] = {}
    values.update(validate(read_file()))

    for name, (want, _default, env_var) in SETTINGS.items():
        if not env_var:
            continue
        raw = os.environ.get(env_var)
        if raw not in (None, ""):
            values[name] = _coerce(name, raw, want)
    return values


def resolve(name: str, override: Any = None) -> Any:
    """One setting, honouring an explicit caller value above every other layer."""
    if name not in SETTINGS:
        raise KeyError(name)
    if override not in (None, ""):
        return _coerce(name, override, SETTINGS[name][0])
    return load()[name]


def write(data: dict[str, Any]) -> None:
    """Replace the config file atomically, so a crash cannot truncate it."""
    validate({k: v for k, v in data.items()})
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(CONFIG_DIR), prefix=".config-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, CONFIG_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge ``patch`` into the file and return the stored result.

    Only the file layer is touched; environment and defaults are not persisted,
    so writing back what :func:`load` returned would not bake them in.
    """
    stored = validate(read_file())
    for name, value in patch.items():
        if name == FIRMWARE_KEY and isinstance(value, dict):
            merged = dict(stored.get(FIRMWARE_KEY) or {})
            merged.update(value)
            stored[FIRMWARE_KEY] = merged
        else:
            stored[name] = value
    stored = validate(stored)
    write(stored)
    return stored


def load_firmware_state() -> dict[str, Any]:
    """Firmware panel state, previously its own ``~/.mpftp/firmware.json``."""
    try:
        return dict(validate(read_file()).get(FIRMWARE_KEY) or {})
    except ConfigError:
        return {}


def save_firmware_state(patch: dict[str, Any]) -> None:
    try:
        update({FIRMWARE_KEY: patch})
    except ConfigError:
        pass
