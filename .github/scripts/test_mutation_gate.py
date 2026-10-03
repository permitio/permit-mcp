"""Tests for mutation_gate.py, which gates the package lines a change edits on mutation score.

Most tests run the gate, and the pinned mutmut, on a planted repository: a base commit, and a
head that adds a function with strong, weak or failing tests.

Run with:
uv run --only-dev pytest -c .github/scripts/pytest.ini .github/scripts/test_mutation_gate.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent
SCRIPT = SCRIPTS / "mutation_gate.py"

sys.path.insert(0, str(SCRIPTS))

import mutation_gate  # noqa: E402 - importable once sys.path has its directory
from mutation_gate import (  # noqa: E402
    GateError,
    Score,
    check_memory_limit,
    parse_diff,
)

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "planted",
    "GIT_AUTHOR_EMAIL": "planted@example.invalid",
    "GIT_COMMITTER_NAME": "planted",
    "GIT_COMMITTER_EMAIL": "planted@example.invalid",
}
PYPROJECT = """\
[project]
name = "planted"
version = "0"

[tool.mutmut]
source_paths = ["src/calc"]
pytest_add_cli_args_test_selection = ["tests"]
"""
BASE_SOURCE = '''\
def double(value: int) -> int:
    """Return twice the value."""
    return value * 2
'''
SIGN = '''

def sign(value: int) -> str:
    """Name the sign of the value."""
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "zero"
'''
BASE_TESTS = """\
from calc import double


def test_double() -> None:
    assert double(2) == 4
"""
STRONG_TESTS = """

from calc import sign


def test_sign() -> None:
    assert sign(1) == "positive"
    assert sign(-1) == "negative"
    assert sign(0) == "zero"
"""
WEAK_TESTS = """

from calc import sign


def test_sign() -> None:
    assert sign(5) == "positive"
"""


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],  # noqa: S607 - git from PATH, as the gate runs it
        env={**GIT_ENV, "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def planted_repo(tmp_path: Path, source: str, tests: str) -> Path:
    """A repository whose HEAD adds `source` to the package and `tests` to its tests."""
    repo = tmp_path / "repo"
    (repo / "src" / "calc").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pyproject.toml").write_text(PYPROJECT)
    (repo / "src" / "calc" / "__init__.py").write_text(BASE_SOURCE)
    (repo / "tests" / "test_calc.py").write_text(BASE_TESTS)
    git(repo, "init", "--quiet")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "base")
    with (repo / "src" / "calc" / "__init__.py").open("a") as out:
        out.write(source)
    with (repo / "tests" / "test_calc.py").open("a") as out:
        out.write(tests)
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "head")
    return repo


def gate(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GITHUB_STEP_SUMMARY": str(repo.parent / "summary.md")}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )


def mutant_names(repo: Path) -> list[str]:
    meta = repo / "mutants" / "src" / "calc" / "__init__.py.meta"
    recorded: dict[str, int] = json.loads(meta.read_text())["exit_code_by_key"]
    return sorted(recorded)


def test_strong_tests_pass_and_only_changed_lines_are_mutated(tmp_path: Path) -> None:
    repo = planted_repo(tmp_path, SIGN, STRONG_TESTS)

    completed = gate(repo, "--base", "HEAD~1", "--threshold", "100", "--workers", "2")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "changed lines in 1 files against HEAD~1" in completed.stdout
    assert "Caught" in completed.stdout
    assert "100.0%, threshold 100%." in completed.stdout
    assert "mutation_gate: PASSED." in completed.stdout
    names = mutant_names(repo)
    assert names
    assert all(name.startswith("calc.x_sign__mutmut_") for name in names), names
    summary = (tmp_path / "summary.md").read_text()
    assert summary.startswith("### Mutation tests\n")
    assert "PASSED." in summary


def test_weak_tests_fail_the_gate_and_show_the_survivors(tmp_path: Path) -> None:
    repo = planted_repo(tmp_path, SIGN, WEAK_TESTS)

    completed = gate(repo, "--base", "HEAD~1", "--threshold", "80", "--workers", "2")

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "mutation_gate: FAILED: the tests caught" in completed.stdout
    assert "below 80%" in completed.stdout
    assert "# calc.x_sign__mutmut_" in completed.stdout
    assert ": survived" in completed.stdout
    assert '-        return "negative"' in completed.stdout, "survivors are shown as diffs"
    summary = (tmp_path / "summary.md").read_text()
    assert "<summary>Surviving mutants</summary>" in summary
    assert "FAILED" in summary


def test_the_same_weak_tests_pass_a_threshold_they_reach(tmp_path: Path) -> None:
    repo = planted_repo(tmp_path, SIGN, WEAK_TESTS)

    completed = gate(repo, "--base", "HEAD~1", "--threshold", "0", "--workers", "1")

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_all_mutates_the_unchanged_lines_too(tmp_path: Path) -> None:
    repo = planted_repo(tmp_path, SIGN, STRONG_TESTS)

    completed = gate(repo, "--all", "--threshold", "0")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "on every line of src/calc" in completed.stdout
    names = mutant_names(repo)
    assert any(name.startswith("calc.x_double__mutmut_") for name in names), names


def test_no_changed_line_passes_without_running_mutmut(tmp_path: Path) -> None:
    repo = planted_repo(tmp_path, "", "\n\ndef test_more() -> None:\n    assert double(0) == 0\n")

    completed = gate(repo, "--base", "HEAD~1", "--threshold", "80")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "No line of src/calc changed against HEAD~1: nothing to mutate." in completed.stdout
    assert not (repo / "mutants").exists()


def test_changed_lines_with_nothing_to_mutate_pass(tmp_path: Path) -> None:
    repo = planted_repo(tmp_path, "\n# A comment.\n", "")

    completed = gate(repo, "--base", "HEAD~1", "--threshold", "80")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "mutation_gate: 0 mutants on 2 changed lines in 1 files" in completed.stdout
    assert "The changed lines hold nothing mutmut mutates." in completed.stdout


def test_a_failing_test_suite_exits_2(tmp_path: Path) -> None:
    failing = "\n\ndef test_broken() -> None:\n    raise AssertionError\n"
    repo = planted_repo(tmp_path, SIGN, STRONG_TESTS + failing)

    completed = gate(repo, "--base", "HEAD~1", "--threshold", "80")

    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert "DID NOT RUN: mutmut exited" in completed.stdout


def test_the_time_limit_stops_mutmut_and_exits_2(tmp_path: Path) -> None:
    repo = planted_repo(tmp_path, SIGN, STRONG_TESTS)

    completed = gate(repo, "--base", "HEAD~1", "--threshold", "80", "--max-minutes", "0.001")

    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert "DID NOT RUN: mutmut did not finish in 0.001 minutes" in completed.stdout


def test_a_base_that_is_not_a_commit_exits_2(tmp_path: Path) -> None:
    repo = planted_repo(tmp_path, SIGN, STRONG_TESTS)

    completed = gate(repo, "--base", "no-such-ref", "--threshold", "80")

    assert completed.returncode == 2
    assert "DID NOT RUN: git rev-parse" in completed.stdout


def test_a_project_without_mutmut_configuration_exits_2(tmp_path: Path) -> None:
    repo = planted_repo(tmp_path, SIGN, STRONG_TESTS)
    (repo / "pyproject.toml").write_text('[project]\nname = "planted"\n')

    completed = gate(repo, "--base", "HEAD~1", "--threshold", "80")

    assert completed.returncode == 2
    assert "cannot read source_paths under [tool.mutmut]" in completed.stdout


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("--threshold", "80"),
        ("--base", "HEAD", "--all", "--threshold", "80"),
        ("--base", "HEAD"),
        ("--base", "HEAD", "--threshold", "101"),
        ("--base", "HEAD", "--threshold", "80", "--workers", "0"),
        ("--base", "HEAD", "--threshold", "80", "--memory-mib", "-1"),
        ("--base", "HEAD", "--threshold", "80", "--max-minutes", "0"),
    ],
    ids=[
        "none",
        "no scope",
        "two scopes",
        "no threshold",
        "threshold above 100",
        "no worker",
        "negative memory",
        "no time",
    ],
)
def test_wrong_arguments_exit_2(tmp_path: Path, args: tuple[str, ...]) -> None:
    completed = gate(tmp_path, *args)
    assert completed.returncode == 2
    assert "DID NOT RUN: wrong arguments" in completed.stdout


def test_the_memory_limit_is_enforced_on_linux_and_refused_elsewhere() -> None:
    # The gate runs with a limit on Linux, as CI does; macOS does not enforce RLIMIT_AS.
    if sys.platform == "linux":
        check_memory_limit(3072)
    else:
        with pytest.raises(GateError, match="pass --memory-mib 0"):
            check_memory_limit(3072)


MEMORY_HUNGRY_TESTS = """

def test_needs_memory() -> None:
    assert len(bytearray(1536 * 1024 * 1024)) > 0
"""


def test_tests_that_fail_under_the_memory_limit_exit_2_before_any_mutant(tmp_path: Path) -> None:
    # Tests that run out of memory would otherwise kill every mutant. Linux enforces the
    # limit, so the suite fails under it; elsewhere the probe already refuses the limit.
    repo = planted_repo(tmp_path, SIGN, STRONG_TESTS + MEMORY_HUNGRY_TESTS)

    completed = gate(repo, "--base", "HEAD~1", "--threshold", "80", "--memory-mib", "1024")

    assert completed.returncode == 2, completed.stdout + completed.stderr
    if sys.platform == "linux":
        assert "memory limit too low for the forked test run" in completed.stdout
        assert "test_needs_memory" in completed.stdout
    else:
        assert "cannot set a" in completed.stdout, "the probe refuses the limit"
        assert "pass --memory-mib 0" in completed.stdout
    assert not (repo / "mutants").exists(), "mutmut does not start"


def test_tests_that_fit_the_memory_limit_let_the_gate_run(tmp_path: Path) -> None:
    repo = planted_repo(tmp_path, SIGN, STRONG_TESTS)

    completed = gate(repo, "--base", "HEAD~1", "--threshold", "80", "--memory-mib", "3072")

    if sys.platform == "linux":
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "mutation_gate: PASSED." in completed.stdout
    else:
        assert completed.returncode == 2
        assert "cannot set a" in completed.stdout, "the probe refuses the limit"
        assert "pass --memory-mib 0" in completed.stdout


@pytest.mark.parametrize(("exit_code", "status"), [(-11, "segfault"), (None, "not checked")])
def test_a_mutant_without_a_result_makes_the_gate_exit_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exit_code: int | None,
    status: str,
) -> None:
    repo = planted_repo(tmp_path, SIGN, STRONG_TESTS)
    planted = {"calc.x_sign__mutmut_1": 1, "calc.x_sign__mutmut_2": exit_code}

    def plant_results(*_args: object, **_kwargs: object) -> None:
        meta = repo / "mutants" / "src" / "calc" / "__init__.py.meta"
        meta.parent.mkdir(parents=True)
        meta.write_text(json.dumps({"exit_code_by_key": planted}))

    monkeypatch.chdir(repo)
    monkeypatch.setattr(mutation_gate, "run_mutmut", plant_results)

    status_code = mutation_gate.main(["--base", "HEAD~1", "--threshold", "80"])

    out = capsys.readouterr().out
    assert status_code == 2, out
    assert f"(1 killed, 1 {status})" in out
    assert "mutation_gate: DID NOT RUN: 1 mutants have no result" in out


def test_a_test_run_failing_under_the_limit_stops_the_gate_before_mutmut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # On any platform: the probe passes and the limit is a no-op, and a failing test stands
    # in for one that ran out of memory.
    failing = "\n\ndef test_out_of_memory() -> None:\n    raise MemoryError\n"
    repo = planted_repo(tmp_path, SIGN, STRONG_TESTS + failing)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(mutation_gate, "check_memory_limit", lambda _mib: None)
    monkeypatch.setattr(mutation_gate, "_limit_memory", lambda _mib: None)

    status = mutation_gate.main(["--base", "HEAD~1", "--threshold", "80", "--memory-mib", "64"])

    out = capsys.readouterr().out
    assert status == 2, out
    assert "memory limit too low for the forked test run" in out
    assert "test_out_of_memory" in out
    assert not (repo / "mutants").exists(), "mutmut does not start"


def test_only_the_shown_survivors_are_diffed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = planted_repo(tmp_path, SIGN, WEAK_TESTS)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(mutation_gate, "SHOWN_SURVIVORS", 2)
    shown: list[str] = []

    def record(name: str, status: str) -> str:
        shown.append(name)
        return f"# {name}: {status}"

    monkeypatch.setattr(mutation_gate, "show", record)

    status = mutation_gate.main(["--base", "HEAD~1", "--threshold", "80", "--workers", "2"])

    out = capsys.readouterr().out
    assert status == 1, out
    assert "The first 2 of 8 missed mutants are shown." in out
    assert len(shown) == 2, "mutmut show runs for the shown survivors only"


def test_a_new_module_with_weak_tests_fails_the_gate(tmp_path: Path) -> None:
    repo = planted_repo(tmp_path, "", "\n")
    (repo / "src" / "calc" / "signs.py").write_text(SIGN.lstrip())
    (repo / "tests" / "test_signs.py").write_text(
        WEAK_TESTS.replace("from calc ", "from calc.signs ")
    )
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "a new module")

    completed = gate(repo, "--base", "HEAD~2", "--threshold", "80", "--workers", "2")

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "changed lines in 1 files against HEAD~2" in completed.stdout
    assert "# calc.signs.x_sign__mutmut_" in completed.stdout


def test_a_memory_limit_too_low_to_start_python_exits_2(tmp_path: Path) -> None:
    repo = planted_repo(tmp_path, SIGN, STRONG_TESTS)

    completed = gate(repo, "--base", "HEAD~1", "--threshold", "80", "--memory-mib", "1")

    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert "DID NOT RUN:" in completed.stdout
    assert not (repo / "mutants").exists(), "mutmut does not start"


def test_parse_diff_reads_added_and_changed_lines() -> None:
    diff = """\
diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -3 +3 @@ def f():
-    return 1
+    return 2
@@ -10,0 +11,3 @@ def g():
+    x = 1
+    y = 2
+    return x + y
@@ -20,2 +23,0 @@ def h():
-    gone = 1
-    also = 2
diff --git a/src/new.py b/src/new.py
--- /dev/null
+++ b/src/new.py
@@ -0,0 +1,2 @@
+A = 1
+B = 2
diff --git a/src/removed_lines_only.py b/src/removed_lines_only.py
--- a/src/removed_lines_only.py
+++ b/src/removed_lines_only.py
@@ -5 +4,0 @@
-gone = True
"""
    assert parse_diff(diff) == {"src/a.py": [3, 11, 12, 13], "src/new.py": [1, 2]}


def test_parse_diff_of_nothing_is_empty() -> None:
    assert parse_diff("") == {}


def test_score_counts_caught_missed_and_unrun() -> None:
    score = Score()
    score.by_status.update(
        {"killed": 6, "timeout": 2, "survived": 1, "no tests": 1, "segfault": 1, "skipped": 1}
    )
    assert (score.caught, score.not_caught, score.unrun) == (8, 2, 2)
    assert score.percent == 80.0
    assert Score().percent == 100.0
