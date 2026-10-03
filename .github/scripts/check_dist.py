#!/usr/bin/env python3
"""Check the wheel and sdist that `uv build` wrote.

Usage: check_dist.py DIST_DIR

The directory must hold exactly one wheel and one sdist of the same version.
The wheel's top level may hold only the package and its .dist-info, so it
installs nothing else into site-packages. The sdist may hold only the package
under src/ and the files uv_build adds beside it. Both must carry py.typed,
which tells type checkers to read the package's annotations, and the wheel must
ship every file of the sdist's package.

Exits 0 when the artifacts pass, 1 when their contents are wrong, and 2 when
there is not exactly one wheel and one sdist to check or one cannot be read.
Stdlib only, so the release workflow can run it on the files it publishes.
"""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path

PACKAGE = "permit_mcp"
TYPED_MARKER = f"{PACKAGE}/py.typed"
# What uv_build writes at the top of an sdist besides src/. pyproject.toml.orig
# is the file as committed; pyproject.toml is the copy uv_build normalised.
SDIST_TOP_FILES = frozenset(
    {"PKG-INFO", "pyproject.toml", "pyproject.toml.orig", "README.md", "LICENSE"}
)

PASS = 0
BAD_CONTENTS = 1
CANNOT_CHECK = 2


class CannotCheckError(Exception):
    """The directory does not hold one readable wheel and one readable sdist."""


def find_artifacts(dist: Path) -> tuple[Path, Path]:
    """Return the one wheel and the one sdist in dist."""
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        found = [path.name for path in wheels + sdists] or "nothing"
        message = f"expected one wheel and one sdist in {dist}, found {found}"
        raise CannotCheckError(message)
    return wheels[0], sdists[0]


def wheel_names(wheel: Path) -> set[str]:
    """The file names in the wheel."""
    try:
        with zipfile.ZipFile(wheel) as archive:
            return {name for name in archive.namelist() if not name.endswith("/")}
    except (OSError, zipfile.BadZipFile) as exc:
        message = f"cannot read {wheel.name}: {exc}"
        raise CannotCheckError(message) from exc


def sdist_names(sdist: Path) -> set[str]:
    """The file names in the sdist, as full paths in the archive."""
    try:
        with tarfile.open(sdist) as archive:
            return {member.name for member in archive.getmembers() if not member.isdir()}
    except (OSError, tarfile.TarError) as exc:
        message = f"cannot read {sdist.name}: {exc}"
        raise CannotCheckError(message) from exc


def check_wheel(wheel: Path, names: set[str], version: str) -> list[str]:
    """Problems with the wheel's contents."""
    dist_info = f"{PACKAGE}-{version}.dist-info"
    allowed = {PACKAGE, dist_info}
    problems = [
        f"{wheel.name} installs {top}/ beside the package; only {sorted(allowed)} may ship"
        for top in sorted({name.split("/")[0] for name in names} - allowed)
    ]
    if TYPED_MARKER not in names:
        problems.append(f"{wheel.name} has no {TYPED_MARKER}")
    if f"{dist_info}/METADATA" not in names:
        problems.append(f"{wheel.name} has no {dist_info}/METADATA")
    return problems


def check_sdist(sdist: Path, names: set[str], version: str) -> list[str]:
    """Problems with the sdist's contents."""
    root = f"{PACKAGE}-{version}/"
    outside = sorted(name for name in names if not name.startswith(root))
    problems = [f"{sdist.name} holds {name} outside {root}" for name in outside]
    inside = sorted(name.removeprefix(root) for name in names if name.startswith(root))
    problems.extend(
        f"{sdist.name} holds {name}; only src/{PACKAGE}/ and metadata may"
        for name in inside
        if name not in SDIST_TOP_FILES and not name.startswith(f"src/{PACKAGE}/")
    )
    if f"{root}src/{TYPED_MARKER}" not in names:
        problems.append(f"{sdist.name} has no src/{TYPED_MARKER}")
    return problems


def check_same_package(wheel: Path, wheel_files: set[str], sdist_files: set[str]) -> list[str]:
    """Problems where the wheel's package differs from the sdist's."""
    in_wheel = {name for name in wheel_files if name.startswith(f"{PACKAGE}/")}
    missing = sorted(sdist_files - in_wheel)
    return [f"{wheel.name} lacks {name}, which the sdist has" for name in missing]


def check(dist: Path) -> list[str]:
    """Every problem with the artifacts in dist; raises CannotCheckError if there are none."""
    wheel, sdist = find_artifacts(dist)
    version = wheel.name.split("-")[1]
    sdist_version = sdist.name.removesuffix(".tar.gz").rpartition("-")[2]
    problems = []
    if not wheel.name.startswith(f"{PACKAGE}-") or not sdist.name.startswith(f"{PACKAGE}-"):
        problems.append(f"{wheel.name} and {sdist.name} must both be {PACKAGE} artifacts")
    if sdist_version != version:
        problems.append(f"{wheel.name} is version {version}, {sdist.name} is {sdist_version}")
    wheel_files = wheel_names(wheel)
    sdist_files = sdist_names(sdist)
    problems += check_wheel(wheel, wheel_files, version)
    problems += check_sdist(sdist, sdist_files, sdist_version)
    package_root = f"{PACKAGE}-{sdist_version}/src/"
    sdist_package = {
        name.removeprefix(package_root) for name in sdist_files if name.startswith(package_root)
    }
    problems += check_same_package(wheel, wheel_files, sdist_package)
    return problems


def main(argv: list[str]) -> int:
    """Check the directory named in argv and return the exit status."""
    if len(argv) != 1:
        print("usage: check_dist.py DIST_DIR", file=sys.stderr)
        return CANNOT_CHECK
    try:
        problems = check(Path(argv[0]))
    except CannotCheckError as exc:
        print(f"::error title=Package contents::{exc}")
        return CANNOT_CHECK
    for problem in problems:
        print(f"::error title=Package contents::{problem}")
    if problems:
        return BAD_CONTENTS
    print(f"The wheel and sdist in {argv[0]} ship {PACKAGE} with py.typed and nothing else.")
    return PASS


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
