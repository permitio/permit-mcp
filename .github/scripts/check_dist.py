#!/usr/bin/env python3
"""Check the wheel and sdist that `uv build` wrote.

Usage: check_dist.py DIST_DIR PYPROJECT

The directory must hold exactly one wheel and one sdist of the same version.
The wheel's top level may hold only the package and its .dist-info, so it
installs nothing else into site-packages. The sdist may hold only the package
under src/ and the files uv_build adds beside it. Both must carry py.typed,
which tells type checkers to read the package's annotations, and the wheel must
ship every file of the sdist's package. The wheel's metadata must declare the
version, requires-python and dependencies of PYPROJECT, so the dependency audit,
which resolves PYPROJECT, scans the ranges the wheel asks for.

Exits 0 when the artifacts pass, 1 when their contents are wrong, and 2 when
there is not exactly one wheel and one sdist to check, or one of them or
PYPROJECT cannot be read. Stdlib only, so the release workflow can run it on
the files it publishes.
"""

from __future__ import annotations

import sys
import tarfile
import tomllib
import zipfile
from email.parser import HeaderParser
from pathlib import Path
from typing import Any

PACKAGE = "permit_mcp"
TYPED_MARKER = f"{PACKAGE}/py.typed"
# What uv_build writes at the top of an sdist besides src/. pyproject.toml.orig
# is the file as committed; pyproject.toml is the copy uv_build normalised.
SDIST_TOP_FILES = frozenset(
    {"PKG-INFO", "pyproject.toml", "pyproject.toml.orig", "README.md", "LICENSE"}
)
# The [project] fields of pyproject.toml and the wheel metadata fields that carry them.
METADATA_FIELDS = {
    "version": "Version",
    "requires-python": "Requires-Python",
    "dependencies": "Requires-Dist",
}

PASS = 0
BAD_CONTENTS = 1
CANNOT_CHECK = 2


class CannotCheckError(Exception):
    """An artifact or pyproject.toml is missing or cannot be read."""


def find_artifacts(dist: Path) -> tuple[Path, Path]:
    """Return the one wheel and the one sdist in dist."""
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        found = [path.name for path in wheels + sdists] or "nothing"
        message = f"expected one wheel and one sdist in {dist}, found {found}"
        raise CannotCheckError(message)
    return wheels[0], sdists[0]


def wheel_contents(wheel: Path, metadata: str) -> tuple[set[str], str]:
    """The file names in the wheel, and the text of its metadata file ("" if it has none)."""
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = {name for name in archive.namelist() if not name.endswith("/")}
            text = archive.read(metadata).decode() if metadata in names else ""
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError) as exc:
        message = f"cannot read {wheel.name}: {exc}"
        raise CannotCheckError(message) from exc
    return names, text


def sdist_names(sdist: Path) -> set[str]:
    """The file names in the sdist, as full paths in the archive."""
    try:
        with tarfile.open(sdist) as archive:
            return {member.name for member in archive.getmembers() if not member.isdir()}
    except (OSError, tarfile.TarError) as exc:
        message = f"cannot read {sdist.name}: {exc}"
        raise CannotCheckError(message) from exc


def read_project(pyproject: Path) -> dict[str, Any]:
    """The [project] table of pyproject, which must set every field in METADATA_FIELDS."""
    try:
        project: dict[str, Any] = tomllib.loads(pyproject.read_text())["project"]
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError) as exc:
        message = f"cannot read [project] from {pyproject}: {exc!r}"
        raise CannotCheckError(message) from exc
    missing = [key for key in METADATA_FIELDS if key not in project]
    if missing:
        message = f"[project] in {pyproject} does not set {missing}"
        raise CannotCheckError(message)
    return project


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


def check_metadata(wheel: Path, metadata: str, project: dict[str, Any]) -> list[str]:
    """Problems where the wheel's metadata differs from the [project] table.

    Whitespace is ignored, and so is the order of the dependencies.
    """
    headers = HeaderParser().parsestr(metadata)
    problems = []
    for key, field in METADATA_FIELDS.items():
        declared = project[key] if isinstance(project[key], list) else [project[key]]
        found = headers.get_all(field) or []
        if sorted("".join(str(value).split()) for value in found) != sorted(
            "".join(str(value).split()) for value in declared
        ):
            problems.append(
                f"{wheel.name} declares {field} {found}; pyproject.toml declares {key} {declared}"
            )
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


def check(dist: Path, pyproject: Path) -> list[str]:
    """Every problem with the artifacts in dist; raises CannotCheckError if there are none."""
    project = read_project(pyproject)
    wheel, sdist = find_artifacts(dist)
    version = wheel.name.split("-")[1]
    sdist_version = sdist.name.removesuffix(".tar.gz").rpartition("-")[2]
    problems = []
    if not wheel.name.startswith(f"{PACKAGE}-") or not sdist.name.startswith(f"{PACKAGE}-"):
        problems.append(f"{wheel.name} and {sdist.name} must both be {PACKAGE} artifacts")
    if sdist_version != version:
        problems.append(f"{wheel.name} is version {version}, {sdist.name} is {sdist_version}")
    wheel_files, metadata = wheel_contents(wheel, f"{PACKAGE}-{version}.dist-info/METADATA")
    sdist_files = sdist_names(sdist)
    problems += check_wheel(wheel, wheel_files, version)
    if metadata:
        problems += check_metadata(wheel, metadata, project)
    problems += check_sdist(sdist, sdist_files, sdist_version)
    package_root = f"{PACKAGE}-{sdist_version}/src/"
    sdist_package = {
        name.removeprefix(package_root) for name in sdist_files if name.startswith(package_root)
    }
    problems += check_same_package(wheel, wheel_files, sdist_package)
    return problems


def main(argv: list[str]) -> int:
    """Check the directory and pyproject.toml named in argv and return the exit status."""
    if len(argv) != len(("DIST_DIR", "PYPROJECT")):
        print("usage: check_dist.py DIST_DIR PYPROJECT", file=sys.stderr)
        return CANNOT_CHECK
    dist, pyproject = argv
    try:
        problems = check(Path(dist), Path(pyproject))
    except CannotCheckError as exc:
        print(f"::error title=Package contents::{exc}")
        return CANNOT_CHECK
    for problem in problems:
        print(f"::error title=Package contents::{problem}")
    if problems:
        return BAD_CONTENTS
    print(
        f"The wheel and sdist in {dist} ship {PACKAGE} with py.typed and nothing else, and"
        f" the wheel declares the version, requires-python and dependencies of {pyproject}."
    )
    return PASS


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
