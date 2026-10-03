#!/usr/bin/env python3
"""Check that a resolved tree holds every direct dependency at its declared floor.

Usage: check_floors.py PYPROJECT REQUIREMENTS

A lowest-direct resolution should pin each dependency in [project].dependencies
to the version its `>=` bound names. When a floor cannot be installed (no
release of it supports a Python the leg runs, say) the resolver quietly picks a
higher version, and the "floor" leg no longer tests the floor. Exits 0 when
every direct dependency is pinned at its floor, 1 when one is pinned elsewhere
or missing, and 2 when a file cannot be read, a dependency has no `>=` floor or
has an environment marker this check cannot evaluate. Stdlib only.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

AT_FLOOR = 0
NOT_AT_FLOOR = 1
CANNOT_CHECK = 2

_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(.*)$")
_FLOOR = re.compile(r">=\s*([^,;\s]+)")
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)")


class CannotCheckError(Exception):
    """An input cannot be read, or a dependency has no floor this check understands."""


def normalize(name: str) -> str:
    """The PEP 503 normal form of a project name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def same_version(left: str, right: str) -> bool:
    """Whether two release versions are equal, ignoring trailing zero components."""

    def trimmed(version: str) -> str:
        parts = version.split(".")
        while len(parts) > 1 and parts[-1] == "0":
            parts.pop()
        return ".".join(parts)

    return trimmed(left) == trimmed(right)


def floors(pyproject: Path) -> dict[str, str]:
    """The `>=` floor of each dependency in [project].dependencies, by normalized name."""
    try:
        dependencies = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"][
            "dependencies"
        ]
    except (OSError, tomllib.TOMLDecodeError, KeyError) as exc:
        message = f"cannot read [project].dependencies from {pyproject}: {exc}"
        raise CannotCheckError(message) from exc
    result: dict[str, str] = {}
    for requirement in dependencies:
        match = _NAME.match(requirement)
        if match is None or ";" in requirement:
            message = f"{requirement!r}: only `name>=floor[,...]` without a marker is supported"
            raise CannotCheckError(message)
        floor = _FLOOR.search(match.group(2))
        if floor is None:
            message = f"{requirement!r} has no `>=` floor"
            raise CannotCheckError(message)
        result[normalize(match.group(1))] = floor.group(1)
    if not result:
        message = f"{pyproject} declares no dependencies"
        raise CannotCheckError(message)
    return result


def pins(requirements: Path) -> dict[str, str]:
    """The `name==version` pins of a compiled requirements file, by normalized name."""
    try:
        lines = requirements.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        message = f"cannot read {requirements}: {exc}"
        raise CannotCheckError(message) from exc
    return {
        normalize(match.group(1)): match.group(2)
        for match in (_PIN.match(line) for line in lines)
        if match is not None
    }


def main(argv: list[str]) -> int:
    """Compare the files named in argv and return the exit status."""
    if len(argv) != 2:  # noqa: PLR2004 - the two file arguments
        print("usage: check_floors.py PYPROJECT REQUIREMENTS", file=sys.stderr)
        return CANNOT_CHECK
    try:
        declared = floors(Path(argv[0]))
        resolved = pins(Path(argv[1]))
    except CannotCheckError as exc:
        print(f"::error title=Floors::{exc}")
        return CANNOT_CHECK
    wrong = [
        f"{name} is {resolved.get(name, 'missing')}, its floor is {floor}"
        for name, floor in sorted(declared.items())
        if not same_version(resolved.get(name, ""), floor)
    ]
    for problem in wrong:
        print(f"::error title=Floors::{argv[1]}: {problem}.")
    if wrong:
        return NOT_AT_FLOOR
    listed = ", ".join(f"{name}=={floor}" for name, floor in sorted(declared.items()))
    print(f"Every direct dependency is at its floor: {listed}.")
    return AT_FLOOR


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
