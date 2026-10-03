"""Tests for check_floors.py, which fails a floor tree that is not at the declared floors.

Run with:
uv run --only-dev pytest -c .github/scripts/pytest.ini .github/scripts/test_check_floors.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent / "check_floors.py"
REPO_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

sys.path.insert(0, str(Path(__file__).parent))

from check_floors import floors  # noqa: E402 - importable once sys.path has its directory


def check(
    tmp_path: Path, dependencies: list[str], requirements: str
) -> subprocess.CompletedProcess[str]:
    pyproject = tmp_path / "pyproject.toml"
    listed = ", ".join(repr(dependency) for dependency in dependencies)
    pyproject.write_text(f'[project]\nname = "x"\nversion = "0"\ndependencies = [{listed}]\n')
    tree = tmp_path / "requirements.txt"
    tree.write_text(requirements)
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(pyproject), str(tree)],
        capture_output=True,
        text=True,
        check=False,
    )


DEPENDENCIES = ["aiohttp>=3.14.3,<4", "mcp>=2.2.0,<3", "Pydantic_Core[extra] >= 2.41"]
FLOOR_TREE = (
    "# compiled\naiohttp==3.14.3\n    # via x\nmcp==2.2.0\npydantic-core==2.41.0\nyarl==1.25.1\n"
)


def test_a_tree_at_the_floors_passes(tmp_path: Path) -> None:
    result = check(tmp_path, DEPENDENCIES, FLOOR_TREE)
    assert result.returncode == 0, result.stdout
    assert "aiohttp==3.14.3, mcp==2.2.0, pydantic-core==2.41." in result.stdout


def test_a_planted_ceiling_tree_fails(tmp_path: Path) -> None:
    result = check(tmp_path, DEPENDENCIES, FLOOR_TREE.replace("mcp==2.2.0", "mcp==2.3.0"))
    assert result.returncode == 1
    assert "mcp is 2.3.0, its floor is 2.2.0." in result.stdout


def test_a_missing_dependency_fails(tmp_path: Path) -> None:
    result = check(tmp_path, DEPENDENCIES, FLOOR_TREE.replace("mcp==2.2.0\n", ""))
    assert result.returncode == 1
    assert "mcp is missing" in result.stdout


@pytest.mark.parametrize(
    ("dependency", "message"),
    [
        ("mcp<3", "has no `>=` floor"),
        ("mcp==2.2.0", "has no `>=` floor"),
        ('mcp>=2.2.0; python_version < "3.13"', "without a marker is supported"),
    ],
)
def test_a_floor_it_cannot_check_exits_2(tmp_path: Path, dependency: str, message: str) -> None:
    result = check(tmp_path, [dependency], FLOOR_TREE)
    assert result.returncode == 2
    assert message in result.stdout


def test_no_dependencies_exits_2(tmp_path: Path) -> None:
    assert check(tmp_path, [], FLOOR_TREE).returncode == 2


def test_an_unreadable_tree_exits_2(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(REPO_PYPROJECT), str(tmp_path / "absent.txt")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2


def test_the_project_s_dependencies_all_have_floors() -> None:
    assert floors(REPO_PYPROJECT)
