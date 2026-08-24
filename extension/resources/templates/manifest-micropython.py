# Frozen Python from workspace user-module repos, plus the MicroPython upstream
# freeze for the active port/board/variant.
#
# mpftp sets ``FROZEN_MANIFEST_UPSTREAM`` to the same manifest file MicroPython
# would have selected (most-specific variant/board/port file). This static file
# includes that path so no generated wrapper is needed.
#
# Optional local overrides: ``manifest-user.py`` (gitignored). Use ``package()`` to
# freeze a tree; paths are relative to the current (workspace) directory. The
# first argument is the import name; that name must be a folder under
# ``base_path``. Example::
#
#     package("pdwidgets", base_path="../pdwidgets/src", opt=3)
#
# freezes ``../pdwidgets/src/pdwidgets/`` as importable ``pdwidgets`` (not
# ``src``).
#
# Child ``*/manifest.py`` inclusion (no hard-coded repo names): include when the
# sibling has ``micropython.mk``, or lacks ``apply_cp_patches.sh`` (skips
# CircuitPython-only trees that would double-freeze shared helpers).

import os

# Optional personal overrides. Missing file is fine; errors inside the file
# must surface (a broad except was silently dropping bad paths).
try:
    include("manifest-user.py")
except OSError:
    pass

for _name in sorted(os.listdir(".")):
    if _name.startswith("."):
        continue
    _path = os.path.join(_name, "manifest.py")
    if not os.path.isfile(_path):
        continue
    _has_mp = os.path.isfile(os.path.join(_name, "micropython.mk"))
    _has_cp_patches = os.path.isfile(os.path.join(_name, "apply_cp_patches.sh"))
    if not (_has_mp or not _has_cp_patches):
        continue
    try:
        include(_path)
    except Exception:
        pass

_upstream = os.environ.get("FROZEN_MANIFEST_UPSTREAM", "").strip()
if not _upstream:
    raise Exception(
        "FROZEN_MANIFEST_UPSTREAM is not set. "
        "Build via mpftp Firmware, or export FROZEN_MANIFEST_UPSTREAM to the "
        "MicroPython port/board/variant manifest.py for this build."
    )
include(_upstream)
