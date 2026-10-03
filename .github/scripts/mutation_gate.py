#!/usr/bin/env python3
"""Run mutmut on the package lines a change adds or edits, and fail when too few are caught.

Usage:
    mutation_gate.py --base REF --threshold PERCENT [options]
    mutation_gate.py --all --threshold PERCENT [options]

Runs in the project environment, from the repository root, since mutmut (pinned in the
dev group) runs the test suite: `uv run --locked python .github/scripts/mutation_gate.py`.
The package is the one path in `source_paths` under [tool.mutmut] in pyproject.toml.

With --base, only the lines that `git diff REF` (the working tree against REF) shows as
added or changed in the package's .py files are mutated: mutmut is told they are the only
covered lines, which is how its mutate_only_covered_lines setting limits mutants. On a
pull request's merge commit, REF is HEAD^1, the base branch. With --all, every line is.

It relies on these mutmut internals; re-check them when bumping mutmut, which Dependabot
proposes on its own:

- `mutmut.__main__.store_lines_covered_by_tests`, which the gate replaces;
- `mutmut.state.state()._covered_lines`, where it puts the changed lines;
- `mutmut.stats.status_by_exit_code`, which names each recorded exit code;
- the `exit_code_by_key` object in each `mutants/**/*.py.meta` file, which it reads;
- `mutmut.__main__.cli.main`, which runs `mutmut run` in its subprocess.

The tests in .github/scripts/test_mutation_gate.py run the pinned mutmut on planted
repositories and fail when one of them changes what the gate sees.

A mutant counts as caught when a test failed on it (killed) or it ran out of time; as
missed when the tests passed (survived) or no test runs the function it is in (no tests).
The score is caught / (caught + missed). A mutant whose test process crashed (mutmut's
"segfault", a signal such as SIGSEGV or SIGKILL), or that mutmut did not run to a result,
is neither: a crash says nothing about the tests, so the gate exits 2 then. The mutmut run
is bounded: --workers test processes at a time, --memory-mib of address space for each
process (RLIMIT_AS; 0 to run without, as on macOS, where the limit is not enforced), and
--max-minutes for the whole run. Before mutmut starts, a probe checks that the limit is
enforced, and the test suite must pass once in a child process under it, so that tests
failing for want of memory cannot count as kills. mutmut's working copy is
mutants/, which the gate deletes first.

The report, surviving mutants with their diffs included, goes to stdout and, when
GITHUB_STEP_SUMMARY is set, to the job summary.

Exits 0 when the score is at least --threshold, no package line changed, or the changed
lines hold nothing mutmut mutates (docstrings, imports, module-level code); 1 when the
score is below --threshold; and 2 when the gate did not run: wrong arguments, a base that
is not a commit, git or mutmut failing, the memory limit not enforced or too low for the
test suite, the time limit reached, or a mutant left without a result.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import tomllib
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

from mutmut.stats import status_by_exit_code

PASSED = 0
FAILED = 1
DID_NOT_RUN = 2

MUTANTS = Path("mutants")
CAUGHT = ("killed", "timeout", "caught by type check")
MISSED = ("survived", "no tests")
SHOWN_SURVIVORS = 30
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


class GateError(Exception):
    """The gate could not run; the message says why."""


@dataclass
class Score:
    """How many of the mutants that ran the tests caught."""

    by_status: Counter[str] = field(default_factory=Counter)
    missed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def caught(self) -> int:
        """Mutants a test failed on, or that ran out of time."""
        return sum(self.by_status[status] for status in CAUGHT)

    @property
    def not_caught(self) -> int:
        """Mutants the tests passed on, or that no test runs."""
        return sum(self.by_status[status] for status in MISSED)

    @property
    def unrun(self) -> int:
        """Mutants without a result: crashed, not checked, skipped or suspicious."""
        return sum(self.by_status.values()) - self.caught - self.not_caught

    @property
    def percent(self) -> float:
        """The caught share of the mutants that ran; 100 when none ran."""
        ran = self.caught + self.not_caught
        return 100.0 * self.caught / ran if ran else 100.0


def parse_diff(text: str) -> dict[str, list[int]]:
    """Return the added and changed lines of each file in a `git diff --unified=0`."""
    lines: dict[str, set[int]] = {}
    path: str | None = None
    for line in text.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            path = None if target == "/dev/null" else target.removeprefix("b/")
            continue
        match = _HUNK.match(line)
        if match and path is not None:
            start = int(match.group(1))
            count = int(match.group(2) or "1")
            lines.setdefault(path, set()).update(range(start, start + count))
    return {path: sorted(numbers) for path, numbers in sorted(lines.items()) if numbers}


def _git(*args: str) -> str:
    """Run git without the user's or the system's configuration, and return its stdout."""
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    git = shutil.which("git")
    if git is None:
        msg = "git is not on PATH"
        raise GateError(msg)
    completed = subprocess.run(  # noqa: S603 - fixed arguments, no shell
        [git, *args], capture_output=True, text=True, env=env, check=False
    )
    if completed.returncode != 0:
        msg = f"git {' '.join(args)} failed: {completed.stderr.strip()}"
        raise GateError(msg)
    return completed.stdout


def changed_lines(base: str, package: str) -> dict[str, list[int]]:
    """Return the lines of the package's .py files that differ from `base`."""
    _git("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    text = _git(
        "diff",
        "--unified=0",
        "--no-color",
        "--no-ext-diff",
        "--no-renames",
        "--diff-filter=d",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        base,
        "--",
        package,
    )
    return {path: lines for path, lines in parse_diff(text).items() if path.endswith(".py")}


def mutmut_config(pyproject: Path) -> tuple[str, list[str]]:
    """Return the source path and the pytest arguments under [tool.mutmut] in pyproject.toml.

    The source path is the one entry of source_paths; the pytest arguments are
    pytest_add_cli_args, then pytest_add_cli_args_test_selection, as mutmut passes them.
    """
    try:
        config = tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["mutmut"]
        paths = config["source_paths"]
    except (OSError, tomllib.TOMLDecodeError, KeyError) as exc:
        msg = f"cannot read source_paths under [tool.mutmut] in {pyproject}: {exc}"
        raise GateError(msg) from exc
    if not isinstance(paths, list) or len(paths) != 1 or not isinstance(paths[0], str):
        msg = f"source_paths under [tool.mutmut] in {pyproject} must name one path"
        raise GateError(msg)
    pytest_args = [
        *config.get("pytest_add_cli_args", []),
        *config.get("pytest_add_cli_args_test_selection", []),
    ]
    if not all(isinstance(arg, str) for arg in pytest_args):
        msg = f"the pytest arguments under [tool.mutmut] in {pyproject} must be strings"
        raise GateError(msg)
    return paths[0], pytest_args


def _limit_memory(mib: int) -> None:
    limit = mib * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def check_memory_limit(mib: int) -> None:
    """Fail unless a process under the limit starts, and cannot allocate the limit."""
    probe = f"print('started', flush=True)\nbytearray({mib * 1024 * 1024})"
    try:
        completed = subprocess.run(  # noqa: S603 - our own interpreter and a fixed program
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=False,
            preexec_fn=lambda: _limit_memory(mib),
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        msg = f"cannot set a {mib} MiB memory limit here ({exc}); pass --memory-mib 0"
        raise GateError(msg) from exc
    if completed.stdout.strip() != "started":
        msg = f"Python does not start under a {mib} MiB memory limit; raise --memory-mib"
        raise GateError(msg)
    if completed.returncode == 0 or "MemoryError" not in completed.stderr:
        msg = (
            f"a {mib} MiB memory limit is not enforced on this system (a probe allocated "
            "past it); pass --memory-mib 0 to run without one"
        )
        raise GateError(msg)


def check_tests_under_limit(
    mib: int, package: str, pytest_args: list[str], max_minutes: float
) -> None:
    """Fail unless the test suite passes in a child process under the memory limit.

    mutmut counts a mutant as killed when its tests fail, so tests that fail for want of
    memory would count as kills. mutmut's workers are forked children under the same
    limit; this run, before any mutant, proves the limit leaves the tests room. As in
    mutmut's runs, the package is imported from the directory that holds it.
    """
    paths = [str(Path(package).parent.resolve()), os.environ.get("PYTHONPATH", "")]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(path for path in paths if path)}
    argv = [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *pytest_args]
    try:
        completed = subprocess.run(  # noqa: S603 - our own interpreter, pytest, the config's args
            argv,
            capture_output=True,
            text=True,
            check=False,
            preexec_fn=lambda: _limit_memory(mib),
            env=env,
            timeout=max_minutes * 60,
        )
    except subprocess.TimeoutExpired:
        msg = f"the test run under the {mib} MiB memory limit did not finish in {max_minutes:g} min"
        raise GateError(msg) from None
    except (subprocess.SubprocessError, OSError) as exc:
        msg = f"could not start the test run under the {mib} MiB memory limit ({exc})"
        raise GateError(msg) from exc
    if completed.returncode != 0:
        output = (completed.stdout + completed.stderr).splitlines(keepends=True)
        msg = (
            f"the test suite failed under the {mib} MiB memory limit, before any mutant: "
            "memory limit too low for the forked test run, or a failing test; the end of "
            f"its output:\n{''.join(output[-40:])}"
        )
        raise GateError(msg)


def run_mutmut(
    lines: dict[str, list[int]] | None, *, workers: int, memory_mib: int, max_minutes: float
) -> None:
    """Run mutmut in a fresh mutants/, on `lines` or, when None, on every line.

    Raises:
        GateError: mutmut ran out of time, or failed other than by finding nothing to
            mutate on `lines`.
    """
    shutil.rmtree(MUTANTS, ignore_errors=True)
    MUTANTS.mkdir()
    lines_file = MUTANTS / "gate-lines.json"
    lines_file.write_text(json.dumps(lines), encoding="utf-8")
    log = MUTANTS / "gate-mutmut.log"
    argv = [sys.executable, __file__, "mutmut", str(lines_file), "--max-children", str(workers)]
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(  # noqa: S603 - this script, with fixed arguments
            argv,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=(lambda: _limit_memory(memory_mib)) if memory_mib else None,  # noqa: PLW1509 - no threads started yet
        )
        try:
            status = process.wait(timeout=max_minutes * 60)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            msg = f"mutmut did not finish in {max_minutes:g} minutes"
            raise GateError(msg) from None
    if status != 0 and not (lines and _hold_no_mutant(lines)):
        tail = "".join(
            log.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)[-40:]
        )
        msg = f"mutmut exited {status}; the end of its output:\n{tail}"
        raise GateError(msg)


def _hold_no_mutant(lines: dict[str, list[int]]) -> bool:
    """Whether mutmut mutated every changed file and made no mutant in any of them.

    mutmut then stops with an error, as no test runs a mutant, before it runs anything.
    """
    for path in lines:
        meta = MUTANTS / f"{path}.meta"
        try:
            if json.loads(meta.read_text(encoding="utf-8"))["exit_code_by_key"]:
                return False
        except (OSError, ValueError, KeyError):
            return False
    return True


def read_score(package: str) -> Score:
    """Count the mutants mutmut recorded under mutants/, by status."""
    score = Score()
    for meta in sorted((MUTANTS / package).rglob("*.py.meta")):
        try:
            recorded = json.loads(meta.read_text(encoding="utf-8"))["exit_code_by_key"]
        except (OSError, ValueError, KeyError) as exc:
            msg = f"cannot read mutmut's results in {meta}: {exc}"
            raise GateError(msg) from exc
        for name, exit_code in sorted(recorded.items()):
            status = status_by_exit_code[exit_code]
            score.by_status[status] += 1
            if status in MISSED:
                score.missed.append((name, status))
    return score


def show(name: str, status: str) -> str:
    """Return mutmut's diff of one mutant against the original code, under its name."""
    completed = subprocess.run(  # noqa: S603 - our own interpreter and mutmut
        [sys.executable, "-m", "mutmut", "show", name],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    shown = completed.stdout.strip() or completed.stderr.strip()
    return shown if shown.startswith(f"# {name}:") else f"# {name}: {status}\n{shown}"


def describe(score: Score, scope: str, threshold: float) -> list[str]:
    """Return the report's lines on what ran and the score."""
    if not score.by_status:
        return [f"0 mutants on {scope}."]
    counts = ", ".join(f"{count} {status}" for status, count in sorted(score.by_status.items()))
    return [
        f"{sum(score.by_status.values())} mutants on {scope} ({counts}).",
        (
            f"Caught {score.caught} of {score.caught + score.not_caught}: "
            f"{score.percent:.1f}%, threshold {threshold:g}%."
        ),
    ]


def write_summary(lines: list[str], survivors: list[str]) -> None:
    """Append the report to the job summary, when GITHUB_STEP_SUMMARY names one."""
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    with Path(summary).open("a", encoding="utf-8") as out:
        out.write("### Mutation tests\n\n")
        out.writelines(f"{line}\n\n" for line in lines)
        if survivors:
            out.write("<details><summary>Surviving mutants</summary>\n\n```diff\n")
            out.writelines(f"{block}\n\n" for block in survivors)
            out.write("```\n\n</details>\n")


def gate(args: argparse.Namespace) -> tuple[int, list[str], list[str]]:
    """Run the gate; return the exit status, the report lines and the survivors' diffs."""
    package, pytest_args = mutmut_config(Path("pyproject.toml"))
    if args.all:
        lines = None
        scope = f"every line of {package}"
    else:
        lines = changed_lines(args.base, package)
        count = sum(len(numbers) for numbers in lines.values())
        if not count:
            unchanged = f"No line of {package} changed against {args.base}: nothing to mutate."
            return PASSED, [unchanged], []
        scope = f"{count} changed lines in {len(lines)} files against {args.base}"
    if args.memory_mib:
        check_memory_limit(args.memory_mib)
        check_tests_under_limit(args.memory_mib, package, pytest_args, args.max_minutes)
    run_mutmut(
        lines, workers=args.workers, memory_mib=args.memory_mib, max_minutes=args.max_minutes
    )
    score = read_score(package)
    report = describe(score, scope, args.threshold)
    if len(score.missed) > SHOWN_SURVIVORS:
        missed = len(score.missed)
        report.append(f"The first {SHOWN_SURVIVORS} of {missed} missed mutants are shown.")
    shown = score.missed[:SHOWN_SURVIVORS]
    survivors = [show(name, status) for name, status in shown]
    if score.unrun:
        report.append(f"DID NOT RUN: {score.unrun} mutants have no result (crashed or not run).")
        return DID_NOT_RUN, report, survivors
    if not score.by_status:
        report.append("The changed lines hold nothing mutmut mutates.")
        return PASSED, report, survivors
    if score.percent < args.threshold:
        report.append(f"FAILED: the tests caught {score.percent:.1f}%, below {args.threshold:g}%.")
        return FAILED, report, survivors
    report.append("PASSED.")
    return PASSED, report, survivors


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse and check the command line; argparse exits on an error."""
    parser = argparse.ArgumentParser(prog="mutation_gate.py", exit_on_error=False)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--base", help="mutate the lines that differ from this commit")
    scope.add_argument("--all", action="store_true", help="mutate every line")
    parser.add_argument("--threshold", type=float, required=True, help="minimum percent caught")
    parser.add_argument("--workers", type=int, default=2, help="test processes at a time")
    parser.add_argument("--memory-mib", type=int, default=0, help="per process; 0: no limit")
    parser.add_argument("--max-minutes", type=float, default=10, help="for the mutmut run")
    args = parser.parse_args(argv)
    if not 0 <= args.threshold <= 100:  # noqa: PLR2004 - a percentage
        parser.error("--threshold must be from 0 to 100")
    if args.workers < 1 or args.memory_mib < 0 or args.max_minutes <= 0:
        parser.error("--workers must be at least 1, --memory-mib 0 or more, --max-minutes > 0")
    return args


def run_mutmut_command(lines_file: str, mutmut_args: list[str]) -> NoReturn:
    """Run mutmut's CLI in this process, limited to the lines in `lines_file` (JSON).

    The file holds null for every line, or {path: [line, ...]}. mutmut reads which lines
    tests cover through store_lines_covered_by_tests when mutate_only_covered_lines is
    set, and mutates only those; the gate puts the changed lines there instead.
    """
    import mutmut.__main__ as mutmut_cli  # noqa: PLC0415 - imported in the mutmut subprocess only
    from mutmut.state import state  # noqa: PLC0415

    lines = json.loads(Path(lines_file).read_text(encoding="utf-8"))
    if lines is not None:

        def store_changed_lines() -> None:
            state()._covered_lines = {  # noqa: SLF001 - the hook mutmut's line filter reads
                str((MUTANTS / path).absolute()): set(numbers) for path, numbers in lines.items()
            }

        mutmut_cli.store_lines_covered_by_tests = store_changed_lines
    if sys.platform == "darwin":
        # mutmut forks a worker for each mutant from a process that has run the tests. On
        # macOS, urllib's system proxy lookup (SystemConfiguration) crashes a forked process
        # with SIGSEGV; aiohttp calls it. The tests clear the proxy variables, so proxies
        # come from the environment only.
        urllib.request.getproxies_macosx_sysconf = dict  # type: ignore[attr-defined]
        urllib.request.proxy_bypass_macosx_sysconf = lambda _host: False  # type: ignore[attr-defined]
    mutmut_cli.cli.main(["run", *mutmut_args], prog_name="mutmut", standalone_mode=True)


def main(argv: list[str]) -> int:
    """Run the gate, print the report and return the exit status."""
    if argv[:1] == ["mutmut"] and len(argv) >= 2:  # noqa: PLR2004 - the subcommand and its file
        run_mutmut_command(argv[1], argv[2:])
    try:
        args = parse_args(argv)
    except (argparse.ArgumentError, SystemExit):
        print("mutation_gate: DID NOT RUN: wrong arguments; see the usage above.")
        return DID_NOT_RUN
    try:
        status, report, survivors = gate(args)
    except GateError as exc:
        status, report, survivors = DID_NOT_RUN, [f"DID NOT RUN: {exc}"], []
    for line in report:
        print(f"mutation_gate: {line}")
    for block in survivors:
        print(block)
    write_summary(report, survivors)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
