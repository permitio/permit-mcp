"""mutmut runs the test suite from its own copy of the project.

That copy holds `source_paths`, the tests and `also_copy` only, so a test that reads another
repository file fails there, and the mutation gate stops with "did not run".
"""

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"


def mutmut_settings() -> dict[str, list[str]]:
    with (ROOT / "pyproject.toml").open("rb") as file:
        settings: dict[str, list[str]] = tomllib.load(file)["tool"]["mutmut"]
    return settings


def ignored_tests(settings: dict[str, list[str]]) -> set[str]:
    prefix = "--ignore=tests/"
    return {
        arg.removeprefix(prefix)
        for arg in settings["pytest_add_cli_args_test_selection"]
        if arg.startswith(prefix)
    }


def first_segments_read_from_root(path: Path) -> set[str]:
    """Return the first path segment of every `ROOT / "..."` expression in a test module."""
    segments: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and isinstance(node.left, ast.Name)
            and node.left.id == "ROOT"
            and isinstance(node.right, ast.Constant)
            and isinstance(node.right.value, str)
        ):
            segments.add(node.right.value.split("/")[0])
    return segments


def test_every_repository_file_the_tests_read_is_copied_for_mutmut() -> None:
    settings = mutmut_settings()
    copied = {"tests", "pyproject.toml", *settings["also_copy"]}
    copied |= {source.split("/")[0] for source in settings["source_paths"]}
    skipped = ignored_tests(settings)
    missing = {
        f"{module.name}: {segment}"
        for module in TESTS.glob("test_*.py")
        if module.name not in skipped
        for segment in first_segments_read_from_root(module)
        if segment not in copied
    }
    assert not missing, f"add these to [tool.mutmut] also_copy: {sorted(missing)}"


def test_the_check_reads_root_paths() -> None:
    assert "CHANGELOG.md" in first_segments_read_from_root(TESTS / "test_docs.py")
