"""The mpremote and pyserial bundled into the VSIX sit inside pyproject's ranges.

The pip package and the VS Code extension must run the same mpremote that CI
tests. pyproject.toml bounds it; extension/vendor-requirements.txt picks the
exact copy the extension carries. This catches a bump of one without the other.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _declared_ranges() -> dict[str, list[tuple[str, str]]]:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"^dependencies = \[(.*?)^\]", text, re.M | re.S)
    assert block, "no dependencies list in pyproject.toml"
    ranges: dict[str, list[tuple[str, str]]] = {}
    for spec in re.findall(r'"([^"]+)"', block.group(1)):
        name = re.match(r"[A-Za-z0-9_.-]+", spec).group(0)
        clauses = re.findall(r"(>=|<=|==|<|>)\s*([0-9.]+)", spec[len(name):])
        ranges[name.lower()] = clauses
    return ranges


def _vendored() -> dict[str, str]:
    text = (ROOT / "extension" / "vendor-requirements.txt").read_text(encoding="utf-8")
    return {
        m.group(1).lower(): m.group(2)
        for m in re.finditer(r"^([A-Za-z0-9_.-]+)==([0-9.]+)", text, re.M)
    }


def _satisfies(version: str, clauses: list[tuple[str, str]]) -> bool:
    ops = {
        ">=": lambda a, b: a >= b,
        "<=": lambda a, b: a <= b,
        ">": lambda a, b: a > b,
        "<": lambda a, b: a < b,
        "==": lambda a, b: a == b,
    }
    v = _key(version)
    return all(ops[op](v, _key(bound)) for op, bound in clauses)


class VendorPinTests(unittest.TestCase):
    def test_every_vendored_package_is_a_bounded_dependency_in_range(self):
        ranges = _declared_ranges()
        vendored = _vendored()
        self.assertEqual(set(vendored), {"mpremote", "pyserial"})
        for name, version in vendored.items():
            with self.subTest(name=name):
                self.assertIn(name, ranges, f"{name} is bundled but not a dependency")
                clauses = ranges[name]
                self.assertTrue(
                    any(op == "<" for op, _ in clauses), f"{name} has no upper bound"
                )
                self.assertTrue(
                    _satisfies(version, clauses),
                    f"bundled {name}=={version} is outside pyproject's {clauses}",
                )

    def test_the_check_fails_outside_the_range(self):
        self.assertFalse(_satisfies("1.30.0", [(">=", "1.28"), ("<", "1.30")]))
        self.assertFalse(_satisfies("1.27.0", [(">=", "1.28"), ("<", "1.30")]))
        self.assertTrue(_satisfies("1.29.0", [(">=", "1.28"), ("<", "1.30")]))


if __name__ == "__main__":
    unittest.main()
