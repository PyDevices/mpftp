#!/usr/bin/env python3
"""Assert every deliverable reports the version in the root VERSION file.

One release version covers the Python distribution, the VS Code extension, and
every future plugin. Nothing generates a version, so the only way they can drift
is a hand edit — which is exactly what this catches, in CI, before a tag exists.

Run from anywhere:  python scripts/check_versions.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _fail(problems: list[str]) -> int:
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    return 1


def main() -> int:
    canonical = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", canonical):
        print(f"VERSION is not X.Y.Z: {canonical!r}", file=sys.stderr)
        return 1

    found: dict[str, str] = {}

    pkg = json.loads((ROOT / "extension" / "package.json").read_text(encoding="utf-8"))
    found["extension/package.json"] = pkg["version"]

    lock = json.loads((ROOT / "extension" / "package-lock.json").read_text(encoding="utf-8"))
    found["extension/package-lock.json"] = lock["version"]
    if lock.get("packages", {}).get(""):
        found['extension/package-lock.json packages[""]'] = lock["packages"][""]["version"]

    plugin = json.loads(
        (ROOT / "integrations" / "claude-code-plugin" / ".claude-plugin" / "plugin.json").read_text(
            encoding="utf-8"
        )
    )
    found["integrations/claude-code-plugin/.claude-plugin/plugin.json"] = plugin["version"]

    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    found[".claude-plugin/marketplace.json plugins[0]"] = marketplace["plugins"][0]["version"]

    # The package itself, read the way a consumer reads it.
    sys.path.insert(0, str(ROOT / "cli" / "src"))
    import mpftp

    found["mpftp.__version__"] = mpftp.__version__

    problems = [
        f"{name} is {value}, but VERSION is {canonical}"
        for name, value in found.items()
        if value != canonical
    ]
    if problems:
        print(f"Version mismatch against VERSION ({canonical}):", file=sys.stderr)
        return _fail(problems)

    print(f"All {len(found)} version declarations agree: {canonical}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
