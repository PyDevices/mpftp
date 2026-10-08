#!/usr/bin/env python3
"""Assert every deliverable reports the version in the root VERSION file.

One release version covers the Python distribution, the VS Code extension, and
every future plugin. Nothing generates a version, so the only way they can drift
is a hand edit — which is exactly what this catches, in CI, before a tag exists.

VERSION is written in PEP 440 form: X.Y.Z for a final release, or a
pre-release X.Y.ZaN, X.Y.ZbN, X.Y.ZrcN or X.Y.Z.devN. The Python package
carries that form as is. The npm manifests and the plugin JSON need SemVer,
so they carry the same version spelled the SemVer way: 0.1.0rc1 becomes
0.1.0-rc.1, 0.1.0.dev2 becomes 0.1.0-dev.2. A final is identical in both.

Run from anywhere:
    python scripts/check_versions.py           # check
    python scripts/check_versions.py --write   # set every declaration from VERSION
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The same pattern the release workflows accept for a version.
VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)(?:(a|b|rc)(\d+)|\.(dev)(\d+))?")

# JSON files whose top-level "version" carries the release, plus the lockfiles'
# root package entry.
TOP_LEVEL = (
    "extension/package.json",
    "extension/package-lock.json",
    "ui/package.json",
    "ui/package-lock.json",
    "integrations/claude-code-plugin/.claude-plugin/plugin.json",
)
LOCKFILES = ("extension/package-lock.json", "ui/package-lock.json")
MARKETPLACE = ".claude-plugin/marketplace.json"


def semver_of(version: str) -> str:
    """The SemVer spelling of a PEP 440 release version."""
    m = VERSION_RE.fullmatch(version)
    if m is None:
        raise ValueError(version)
    base, pre, pre_n, dev, dev_n = m.groups()
    if pre:
        return f"{base}-{pre}.{pre_n}"
    if dev:
        return f"{base}-dev.{dev_n}"
    return base


def _load(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def _dump(rel: str, data: dict) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    (ROOT / rel).write_text(text, encoding="utf-8")


def write(semver: str) -> None:
    for rel in TOP_LEVEL:
        data = _load(rel)
        data["version"] = semver
        if rel in LOCKFILES and data.get("packages", {}).get(""):
            data["packages"][""]["version"] = semver
        _dump(rel, data)
    data = _load(MARKETPLACE)
    data["plugins"][0]["version"] = semver
    _dump(MARKETPLACE, data)


def declared() -> dict[str, str]:
    found: dict[str, str] = {}
    for rel in TOP_LEVEL:
        data = _load(rel)
        found[rel] = data["version"]
        if rel in LOCKFILES and data.get("packages", {}).get(""):
            found[f'{rel} packages[""]'] = data["packages"][""]["version"]
    found[f"{MARKETPLACE} plugins[0]"] = _load(MARKETPLACE)["plugins"][0]["version"]
    return found


def main(argv: list[str]) -> int:
    canonical = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not VERSION_RE.fullmatch(canonical):
        print(
            f"VERSION is not X.Y.Z, X.Y.Z{{a|b|rc}}N or X.Y.Z.devN: {canonical!r}",
            file=sys.stderr,
        )
        return 1
    semver = semver_of(canonical)

    if argv == ["--write"]:
        write(semver)
    elif argv:
        print(__doc__, file=sys.stderr)
        return 2

    expected = {name: semver for name in declared()}
    found = declared()

    # The package itself, read the way a consumer reads it.
    sys.path.insert(0, str(ROOT / "cli" / "src"))
    import mpftp

    found["mpftp.__version__"] = mpftp.__version__
    expected["mpftp.__version__"] = canonical

    problems = [
        f"{name} is {value}, but VERSION {canonical} wants {expected[name]}"
        for name, value in found.items()
        if value != expected[name]
    ]
    if problems:
        print(f"Version mismatch against VERSION ({canonical}):", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    spelled = canonical if semver == canonical else f"{canonical} (npm and plugins: {semver})"
    print(f"All {len(found)} version declarations agree: {spelled}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
