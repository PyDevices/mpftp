"""mpftp — MicroPython and CircuitPython board tools.

Importing this package has no side effects: nothing is spawned, no socket is
opened, and no file is written. Entry points live in :mod:`mpftp.cli`,
:mod:`mpftp.sidecar` and :mod:`mpftp.firmware`.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["__version__"]


def _resolve_version() -> str:
    """Version of this copy of mpftp.

    Two deployments have to agree. A pip install of ``pydevices-mpftp`` carries
    real distribution metadata. The copy vendored inside the VSIX never gets
    installed, so it ships the repository's root VERSION file alongside the
    package instead. Neither path generates a source file, so nothing can drift
    from the canonical VERSION.
    """
    try:
        from importlib.metadata import version

        return version("pydevices-mpftp")
    except Exception:  # PackageNotFoundError, or no metadata at all
        pass

    here = Path(__file__).resolve()
    candidates = (
        here.with_name("VERSION"),  # staged next to the vendored package
        here.parents[3] / "VERSION",  # cli/src/mpftp/__init__.py -> repo root
    )
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text

    return "0.0.0+unknown"


__version__ = _resolve_version()
