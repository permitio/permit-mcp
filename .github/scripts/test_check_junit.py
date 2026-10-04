"""Tests for check_junit.py, which fails a test leg that skipped a test or ran none.

Run with:
uv run --only-dev pytest -c .github/scripts/pytest.ini .github/scripts/test_check_junit.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent / "check_junit.py"


def check(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False
    )


def report(tmp_path: Path, tests: int, *, skipped: int = 0, failures: int = 0) -> str:
    path = tmp_path / "junit.xml"
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
        f'<testsuite name="pytest" errors="0" failures="{failures}" skipped="{skipped}" '
        f'tests="{tests}" time="0.1"></testsuite></testsuites>'
    )
    return str(path)


def pytest_report(tmp_path: Path, source: str) -> str:
    """The JUnit report of a real pytest run of one planted test module."""
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_planted.py").write_text(source)
    junit = tmp_path / "junit.xml"
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}"],
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    return str(junit)


def test_a_report_where_every_test_ran_passes(tmp_path: Path) -> None:
    result = check(report(tmp_path, 283))
    assert result.returncode == 0
    assert "All 283 tests ran and passed." in result.stdout


def test_a_planted_skip_fails_with_1(tmp_path: Path) -> None:
    result = check(report(tmp_path, 283, skipped=1))
    assert result.returncode == 1
    assert "::error title=Test report::Of 283 tests, 1 skipped." in result.stdout


def test_a_failure_fails_with_1(tmp_path: Path) -> None:
    result = check(report(tmp_path, 10, failures=2))
    assert result.returncode == 1
    assert "2 failures" in result.stdout


def test_a_real_pytest_report_with_a_skip_fails(tmp_path: Path) -> None:
    junit = pytest_report(
        tmp_path,
        "import pytest\n\n"
        "def test_runs():\n    pass\n\n"
        "@pytest.mark.skip(reason='planted')\ndef test_skipped():\n    pass\n",
    )
    result = check(junit)
    assert result.returncode == 1
    assert "Of 2 tests, 1 skipped." in result.stdout


def test_a_real_pytest_report_with_no_skip_passes(tmp_path: Path) -> None:
    result = check(pytest_report(tmp_path, "def test_runs():\n    pass\n"))
    assert result.returncode == 0
    assert "All 1 tests ran" in result.stdout


def test_a_real_pytest_report_of_no_tests_exits_2(tmp_path: Path) -> None:
    result = check(pytest_report(tmp_path, "VALUE = 1\n"))
    assert result.returncode == 2
    assert "holds no test" in result.stdout


def test_zero_tests_exits_2(tmp_path: Path) -> None:
    result = check(report(tmp_path, 0))
    assert result.returncode == 2


@pytest.mark.parametrize(
    "content",
    ["", "not xml", '<testsuite tests="many"/>'],
    ids=["empty", "not xml", "bad count"],
)
def test_an_unreadable_report_exits_2(tmp_path: Path, content: str) -> None:
    path = tmp_path / "junit.xml"
    path.write_text(content)
    result = check(str(path))
    assert result.returncode == 2
    assert "Could not read" in result.stdout


def test_a_missing_report_exits_2(tmp_path: Path) -> None:
    result = check(str(tmp_path / "absent.xml"))
    assert result.returncode == 2


def test_a_bare_testsuite_root_is_read(tmp_path: Path) -> None:
    path = tmp_path / "junit.xml"
    path.write_text('<testsuite tests="3" skipped="1"/>')
    assert check(str(path)).returncode == 1


def test_no_argument_exits_2() -> None:
    assert check().returncode == 2
