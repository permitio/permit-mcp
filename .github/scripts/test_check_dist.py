"""Tests for check_dist.py, against planted wheels and sdists.

Run with:
uv run --only-dev pytest -c .github/scripts/pytest.ini .github/scripts/test_check_dist.py
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent / "check_dist.py"

PACKAGE_FILES = ["__init__.py", "server.py", "py.typed"]
DIST_INFO = ["METADATA", "WHEEL", "RECORD", "entry_points.txt", "licenses/LICENSE"]
SDIST_TOP = ["PKG-INFO", "pyproject.toml", "pyproject.toml.orig", "README.md", "LICENSE"]
PYPROJECT = """[project]
name = "permit-mcp"
version = "1.2.3"
requires-python = ">=3.11"
dependencies = ["aiohttp>=3.14.3,<4", "mcp >= 2.2.0, < 3"]
"""
# As uv_build writes it: other header order, other whitespace.
METADATA = """Metadata-Version: 2.4
Name: permit-mcp
Version: 1.2.3
Requires-Dist: mcp>=2.2.0,<3
Requires-Dist: aiohttp>=3.14.3,<4
Requires-Python: >=3.11

The README.
"""


def check(dist: Path, pyproject: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run check_dist.py on dist and, unless given another, the dist fixture's pyproject.toml."""
    pyproject = pyproject or dist.parent / "pyproject.toml"
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(dist), str(pyproject)],
        capture_output=True,
        text=True,
        check=False,
    )


def wheel_files(version: str = "1.2.3") -> list[str]:
    return [f"permit_mcp/{name}" for name in PACKAGE_FILES] + [
        f"permit_mcp-{version}.dist-info/{name}" for name in DIST_INFO
    ]


def sdist_files(version: str = "1.2.3") -> list[str]:
    root = f"permit_mcp-{version}"
    return [f"{root}/{name}" for name in SDIST_TOP] + [
        f"{root}/src/permit_mcp/{name}" for name in PACKAGE_FILES
    ]


def write_wheel(
    dist: Path, names: list[str], version: str = "1.2.3", metadata: str = METADATA
) -> None:
    with zipfile.ZipFile(dist / f"permit_mcp-{version}-py3-none-any.whl", "w") as archive:
        for name in names:
            archive.writestr(name, metadata if name.endswith(".dist-info/METADATA") else "")


def write_sdist(dist: Path, names: list[str], version: str = "1.2.3") -> None:
    with tarfile.open(dist / f"permit_mcp-{version}.tar.gz", "w:gz") as archive:
        for name in names:
            archive.addfile(tarfile.TarInfo(name), io.BytesIO(b""))


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    path = tmp_path / "dist"
    path.mkdir()
    (path / ".gitignore").write_text("*")
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    return path


def test_a_good_wheel_and_sdist_pass(dist: Path) -> None:
    write_wheel(dist, wheel_files())
    write_sdist(dist, sdist_files())
    result = check(dist)
    assert result.returncode == 0, result.stdout
    assert "ship permit_mcp with py.typed and nothing else" in result.stdout


def test_a_planted_top_level_tests_package_in_the_wheel_fails(dist: Path) -> None:
    write_wheel(dist, [*wheel_files(), "tests/__init__.py"])
    write_sdist(dist, sdist_files())
    result = check(dist)
    assert result.returncode == 1
    assert "installs tests/ beside the package" in result.stdout


def test_a_wheel_without_py_typed_fails(dist: Path) -> None:
    write_wheel(dist, [name for name in wheel_files() if not name.endswith("py.typed")])
    write_sdist(dist, sdist_files())
    result = check(dist)
    assert result.returncode == 1
    assert "has no permit_mcp/py.typed" in result.stdout
    assert "lacks permit_mcp/py.typed, which the sdist has" in result.stdout


def test_a_wheel_without_metadata_fails(dist: Path) -> None:
    write_wheel(dist, [name for name in wheel_files() if not name.endswith("METADATA")])
    write_sdist(dist, sdist_files())
    assert "has no permit_mcp-1.2.3.dist-info/METADATA" in check(dist).stdout


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("permit_mcp-1.2.3/tests/test_server.py", "holds tests/test_server.py"),
        ("permit_mcp-1.2.3/.github/workflows/ci.yml", "holds .github/workflows/ci.yml"),
        ("permit_mcp-1.2.3/src/other/__init__.py", "holds src/other/__init__.py"),
        ("elsewhere/file.txt", "holds elsewhere/file.txt outside permit_mcp-1.2.3/"),
    ],
)
def test_planted_junk_in_the_sdist_fails(dist: Path, extra: str, message: str) -> None:
    write_wheel(dist, wheel_files())
    write_sdist(dist, [*sdist_files(), extra])
    result = check(dist)
    assert result.returncode == 1
    assert message in result.stdout


def test_an_sdist_without_py_typed_fails(dist: Path) -> None:
    write_wheel(dist, wheel_files())
    write_sdist(dist, [name for name in sdist_files() if not name.endswith("py.typed")])
    result = check(dist)
    assert result.returncode == 1
    assert "has no src/permit_mcp/py.typed" in result.stdout


def test_a_module_missing_from_the_wheel_fails(dist: Path) -> None:
    write_wheel(dist, [name for name in wheel_files() if not name.endswith("server.py")])
    write_sdist(dist, sdist_files())
    result = check(dist)
    assert result.returncode == 1
    assert "lacks permit_mcp/server.py, which the sdist has" in result.stdout


def test_different_versions_fail(dist: Path) -> None:
    write_wheel(dist, wheel_files())
    write_sdist(dist, sdist_files("1.2.4"), version="1.2.4")
    result = check(dist)
    assert result.returncode == 1
    assert "is version 1.2.3" in result.stdout


@pytest.mark.parametrize(
    ("wheels", "sdists"),
    [(0, 0), (1, 0), (0, 1), (2, 1)],
    ids=["none", "no sdist", "no wheel", "two"],
)
def test_other_than_one_wheel_and_one_sdist_exits_2(dist: Path, wheels: int, sdists: int) -> None:
    for index in range(wheels):
        write_wheel(dist, wheel_files(f"1.2.{index}"), version=f"1.2.{index}")
    for index in range(sdists):
        write_sdist(dist, sdist_files(f"1.2.{index}"), version=f"1.2.{index}")
    result = check(dist)
    assert result.returncode == 2
    assert "expected one wheel and one sdist" in result.stdout


def test_a_corrupt_wheel_exits_2(dist: Path) -> None:
    (dist / "permit_mcp-1.2.3-py3-none-any.whl").write_text("not a zip")
    write_sdist(dist, sdist_files())
    result = check(dist)
    assert result.returncode == 2
    assert "cannot read permit_mcp-1.2.3-py3-none-any.whl" in result.stdout


def test_a_missing_directory_exits_2(dist: Path) -> None:
    result = check(dist / "absent", dist.parent / "pyproject.toml")
    assert result.returncode == 2
    assert "expected one wheel and one sdist" in result.stdout


@pytest.mark.parametrize(
    ("planted", "message"),
    [
        (
            METADATA.replace("Version: 1.2.3", "Version: 1.2.4"),
            "declares Version ['1.2.4']; pyproject.toml declares version ['1.2.3']",
        ),
        (
            METADATA.replace("Requires-Python: >=3.11", "Requires-Python: >=3.10"),
            "declares Requires-Python ['>=3.10']",
        ),
        (
            METADATA.replace("mcp>=2.2.0,<3", "mcp>=2.1.0,<3"),
            "declares Requires-Dist ['mcp>=2.1.0,<3', 'aiohttp>=3.14.3,<4']",
        ),
        (
            METADATA.replace("Requires-Dist: aiohttp>=3.14.3,<4\n", ""),
            "declares Requires-Dist ['mcp>=2.2.0,<3']",
        ),
        (
            METADATA.replace("Requires-Python", "Requires-Dist: httpx\nRequires-Python"),
            "declares Requires-Dist ['mcp>=2.2.0,<3', 'aiohttp>=3.14.3,<4', 'httpx']",
        ),
    ],
    ids=["version", "requires-python", "a floor", "a missing dependency", "an extra dependency"],
)
def test_metadata_that_differs_from_pyproject_fails(dist: Path, planted: str, message: str) -> None:
    write_wheel(dist, wheel_files(), metadata=planted)
    write_sdist(dist, sdist_files())
    result = check(dist)
    assert result.returncode == 1
    assert message in result.stdout


@pytest.mark.parametrize(
    "pyproject",
    [
        None,
        "not = [toml",
        '[tool.uv]\nexclude-newer = "7 days"\n',
        PYPROJECT.replace('requires-python = ">=3.11"\n', ""),
    ],
    ids=["missing", "not TOML", "no [project]", "no requires-python"],
)
def test_an_unreadable_pyproject_exits_2(dist: Path, pyproject: str | None) -> None:
    path = dist.parent / "pyproject.toml"
    if pyproject is None:
        path.unlink()
    else:
        path.write_text(pyproject)
    write_wheel(dist, wheel_files())
    write_sdist(dist, sdist_files())
    result = check(dist)
    assert result.returncode == 2
    assert "pyproject.toml" in result.stdout


@pytest.mark.parametrize("argv", [[], ["dist"], ["dist", "pyproject.toml", "extra"]])
def test_other_than_two_arguments_exits_2(argv: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *argv], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert "usage: check_dist.py DIST_DIR PYPROJECT" in result.stderr


def test_the_built_wheel_and_sdist_pass(tmp_path: Path) -> None:
    """The artifacts `uv build` writes from this repository, as CI and the release build them."""
    repo = SCRIPT.parents[2]
    uv = shutil.which("uv")
    assert uv is not None, "uv is not on PATH; uv run puts it there"
    subprocess.run(
        [uv, "build", "--no-sources", "--quiet", "--out-dir", str(tmp_path / "dist"), str(repo)],
        check=True,
    )
    result = check(tmp_path / "dist", repo / "pyproject.toml")
    assert result.returncode == 0, result.stdout
