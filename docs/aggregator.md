# Workspace aggregators and user modules

mpftp builds MicroPython with optional **user modules** discovered from the
**firmware workspace** (the parent of the `micropython/` checkout, or the folder
you chose as `mpftp.workspacePath`).

## Layout

```
<firmware-workspace>/
  micropython/                 # or symlink
  micropython.cmake            # CMake AGGREGATOR (for USER_C_MODULES)
  manifest-micropython.py      # frozen-Python AGGREGATOR (usual)
  graphics/                    # example native usermod
    micropython.cmake          # and/or micropython.mk
    manifest.py                # optional frozen Python for this module
  pdwidgets/                   # example frozen-only package
    manifest.py
  usdl2/
    micropython.cmake
    ...
  esp-idf -> ...               # optional SDK symlink
  emsdk -> ...
```

Create missing aggregators from the Firmware UI (**Create stubs…**) or copy
[`resources/templates/micropython.cmake`](../resources/templates/micropython.cmake)
and [`resources/templates/manifest-micropython.py`](../resources/templates/manifest-micropython.py).

## CMake aggregator (`micropython.cmake`)

When mpftp builds with `USER_C_MODULES=<firmware-workspace>`, MicroPython’s
CMake includes the **workspace-root** `micropython.cmake`. That file is an
**aggregator only**: it does not define modules itself. It finds every
`*/micropython.cmake` under the workspace (following symlinks) and `include()`s
them.

## What counts as a module (`firmware cmods`)

A **module** is a **direct child directory** of the firmware workspace whose
**root** contains **any** of:

- `micropython.cmake`, and/or
- `micropython.mk`, and/or
- `manifest.py`

Make/cmake and `manifest.py` are **independent**:

| Package type | Needs in module root |
|--------------|----------------------|
| Native usermod | `micropython.cmake` and/or `micropython.mk` |
| Frozen Python only | `manifest.py` alone is enough |
| Both | Any combination in the same root |

Also:

- The workspace-root aggregators are **not** modules.
- Hidden directories (`.git`, …) are skipped.
- mpftp enables `USER_C_MODULES` when the cmake aggregator is present (or Make
  usermods are found). It does **not** gate modules by board compatibility —
  each module’s own `micropython.cmake` / `micropython.mk` must opt in/out and
  resolve its own dependencies (for example SDL2 for a desktop usermod).
- Frozen-only modules are included via the root `manifest-micropython.py`
  aggregator when that file exists.

## Module root contract

| File | Role |
|------|------|
| `micropython.cmake` | CMake usermod registration (esp32 and other CMake ports) |
| `micropython.mk` | Make-based usermod registration (Make ports) |
| `manifest.py` | Frozen Python for this module (`freeze(...)`, `package(...)`, …) |

**At least one** of those three must sit in the **module root** (not only nested
deeper) for `./scripts/mpftp firmware cmods` to list the directory.

## Frozen Python aggregator (`manifest-micropython.py`)

The workspace-root `manifest-micropython.py`:

1. Optionally includes `manifest-user.py` (local overrides).
2. Includes each child `*/manifest.py` when the sibling has `micropython.mk` or
   lacks `apply_cp_patches.sh` (skips CircuitPython-only trees).
3. Includes upstream via `FROZEN_MANIFEST_UPSTREAM` (mpftp sets this to the
   port/board/variant manifest MicroPython would have used).

Without a root `manifest-micropython.py`, the build will not freeze workspace
Python packages even if child `manifest.py` files exist.

## Checklist when changing a usermod

1. Confirm the module directory is a **sibling of `micropython/`** (or symlinked
   there) and has `micropython.cmake`, `micropython.mk`, and/or `manifest.py`
   at its root.
2. `./scripts/mpftp firmware cmods` — the module name should appear.
3. Rebuild the target port/board/variant; flash; connect; exercise from the REPL.
4. Pure Python that is **not** frozen can be `put` to the board without a
   firmware rebuild; **native / frozen** changes need Build + Flash.

See also [AGENTS.md](../AGENTS.md) for board/CLI workflows and
[user-guide.md](user-guide.md) for getting started.
