"""Tests for the bash gates in .github/workflows/ci.yml.

Each test reads a step's `run:` block and `env:` from ci.yml with yq, and runs it
with the workflow's default shell against planted job results, planted copies
of the workflow and stand-ins for the tools it calls. Others pin how the gates
are wired, run audit-deps.sh, and run the pinned gitleaks and Trivy on planted
repositories. The tests need bash, git, jq, yq (mikefarah v4), shellcheck, and
the pinned gitleaks and Trivy on PATH; the audit-scripts job installs the last
two, and GitHub's ubuntu-24.04 runners have the rest.

Run with:
uv run --only-dev pytest -c .github/scripts/pytest.ini .github/scripts/test_ci_checks.py
"""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import shutil
import stat
import string
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = Path(__file__).resolve().parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
AUDIT_DEPS = SCRIPTS / "audit-deps.sh"
CI_STEP = ("ci", "Check the needed jobs")
NEEDS_CHECK_STEP = ("prek", "Check that CI needs every job")
HOOKS_STEP = ("prek", "Run hooks")
VERSIONS_STEP = ("tests", "Check the installed runtime versions")
INSTALL_STEP = ("tests", "Install from the ranges in pyproject.toml")
AUDIT_GATE_STEP = ("audit", "Gate on fixable HIGH and CRITICAL advisories")
GITLEAKS_STEP = ("gitleaks", "Scan the history")
SURFACE_STEP = ("tests", "Report tool surface changes")
MUTATION_STEP = ("mutation", "Run the mutation tests on the changed lines")
PULL_REQUEST_IF = "github.event_name == 'pull_request'"
# CI's own scheduled and manual runs: never a pull request, a push or a run release.yml calls.
SCHEDULED_IF = (
    "github.workflow == 'CI' &&"
    " (github.event_name == 'schedule' || github.event_name == 'workflow_dispatch')"
)
DEFAULT_SHELL = "bash --noprofile --norc -euo pipefail {0}"


sys.path.insert(0, str(SCRIPTS))

from check_floors import floors  # noqa: E402 - importable once sys.path has its directory


def tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        pytest.fail(f"{name} is not on PATH; these tests run the workflow's bash, which needs it")
    return path


def read_workflow(path: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [tool("yq"), "-o=json", ".", str(path)], capture_output=True, text=True, check=True
    )
    workflow: dict[str, Any] = json.loads(completed.stdout)
    return workflow


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return read_workflow(WORKFLOW)


@pytest.fixture(scope="module")
def needed(workflow: dict[str, Any]) -> list[str]:
    needs: list[str] = workflow["jobs"]["ci"]["needs"]
    return needs


def find_step(workflow: dict[str, Any], job_and_step: tuple[str, str]) -> dict[str, Any]:
    job, name = job_and_step
    steps = [step for step in workflow["jobs"][job]["steps"] if step.get("name") == name]
    assert len(steps) == 1, f"expected one step named {name!r} in job {job!r}, found {len(steps)}"
    step: dict[str, Any] = steps[0]
    return step


def stand_in(directory: Path, name: str, script: str) -> Path:
    """Write an executable bash script named `name` into directory."""
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_text(f"#!/usr/bin/env bash\n{script}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def run_step(
    workflow: dict[str, Any],
    job_and_step: tuple[str, str],
    tmp_path: Path,
    env: dict[str, str],
    *,
    cwd: Path = REPO_ROOT,
) -> subprocess.CompletedProcess[str]:
    """Run a step as GitHub does: the default shell, the workflow's, job's and step's env.

    tmp_path is RUNNER_TEMP, and tmp_path/bin goes first on PATH, for stand-ins. cwd
    is the checkout, the repository root unless a test plants another.
    """
    job = workflow["jobs"][job_and_step[0]]
    step = find_step(workflow, job_and_step)
    assert "shell" not in step, "these steps run with the workflow's default shell"
    script = tmp_path / "step.sh"
    script.write_text(step["run"], encoding="utf-8")
    argv = workflow["defaults"]["run"]["shell"].split()
    argv = [tool("bash") if arg == "bash" else str(script) if arg == "{0}" else arg for arg in argv]
    declared = {**workflow.get("env", {}), **job.get("env", {}), **step.get("env", {})}
    return subprocess.run(
        argv,
        env={
            "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "HOME": os.environ["HOME"],
            **{key: str(value) for key, value in declared.items()},
            **env,
        },
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


# --- the workflow's shape ------------------------------------------------------


def test_steps_run_with_errexit_nounset_and_pipefail(workflow: dict[str, Any]) -> None:
    assert workflow["defaults"]["run"]["shell"] == DEFAULT_SHELL


def test_the_workflow_grants_nothing_by_default(workflow: dict[str, Any]) -> None:
    assert workflow["permissions"] == {}


def test_every_job_needs_at_most_contents_read(workflow: dict[str, Any]) -> None:
    # A release workflow calls this one, and a called job gets at most what the
    # caller grants: a caller that grants contents: read must cover every job.
    for name, job in workflow["jobs"].items():
        assert set(job["permissions"].items()) <= {("contents", "read")}, name


def test_every_job_has_a_timeout(workflow: dict[str, Any]) -> None:
    for name, job in workflow["jobs"].items():
        limit = 30 if name == "mutation" else 15
        assert job["timeout-minutes"] <= limit, name


def test_every_checkout_drops_its_credentials(workflow: dict[str, Any]) -> None:
    checkouts = [
        step
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert checkouts
    for step in checkouts:
        assert step["with"]["persist-credentials"] is False


def test_the_workflow_can_be_called(workflow: dict[str, Any]) -> None:
    assert "workflow_call" in workflow["on"]


# --- the CI job ----------------------------------------------------------------


def results(needed: list[str], overrides: dict[str, str] | None = None) -> str:
    """`toJSON(needs)` for the given jobs, each a success unless `overrides` says otherwise."""
    overrides = overrides or {}
    return json.dumps(
        {job: {"result": overrides.get(job, "success"), "outputs": {}} for job in needed}
    )


def run_ci(  # noqa: PLR0913 - the step's inputs, keyword-only past the event
    workflow: dict[str, Any],
    tmp_path: Path,
    needs: str,
    event: str,
    advisory: str | None = None,
    *,
    workflow_name: str = "CI",
) -> subprocess.CompletedProcess[str]:
    env = {"NEEDS": needs, "EVENT": event, "WORKFLOW": workflow_name}
    if advisory is not None:
        env["ADVISORY_JOBS"] = advisory
    return run_step(workflow, CI_STEP, tmp_path, env)


def test_ci_is_named_ci_and_runs_whatever_happened_to_its_needs(workflow: dict[str, Any]) -> None:
    ci = workflow["jobs"]["ci"]
    assert ci["name"] == "CI"
    assert ci["if"] == "always()"


def test_only_the_e2e_suite_is_advisory(workflow: dict[str, Any]) -> None:
    assert find_step(workflow, CI_STEP)["env"]["ADVISORY_JOBS"] == "e2e"


def test_scheduled_jobs_are_the_needed_jobs_that_run_in_scheduled_and_manual_runs_only(
    workflow: dict[str, Any], needed: list[str]
) -> None:
    scheduled = [job for job in needed if workflow["jobs"][job].get("if") == SCHEDULED_IF]
    assert find_step(workflow, CI_STEP)["env"]["SCHEDULED_JOBS"].split() == scheduled == ["e2e"]
    assert find_step(workflow, CI_STEP)["env"]["WORKFLOW"] == "${{ github.workflow }}"


@pytest.mark.parametrize("result", ["failure", "cancelled"])
@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_ci_warns_when_the_e2e_suite_failed_in_a_run_it_belongs_to(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path, result: str, event: str
) -> None:
    completed = run_ci(workflow, tmp_path, results(needed, {"e2e": result}), event)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"::warning title=CI::Advisory job did not succeed: e2e {result}" in completed.stdout
    assert "::notice" not in completed.stdout
    assert "::error" not in completed.stdout


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_ci_fails_when_the_e2e_suite_skipped_in_a_run_it_belongs_to(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path, event: str
) -> None:
    completed = run_ci(workflow, tmp_path, results(needed, {"e2e": "skipped"}), event)
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert (
        f"::error title=CI::e2e was skipped on CI's own {event} run, which it must run in:"
        " its if: no longer matches this run."
    ) in completed.stdout
    assert "::error title=CI::Jobs that did not succeed: e2e skipped" in completed.stdout
    assert "::warning" not in completed.stdout
    assert "::notice" not in completed.stdout


def test_ci_names_a_skipped_e2e_suite_beside_the_other_jobs_that_did_not_succeed(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path
) -> None:
    needs = results(needed, {"e2e": "skipped", "audit": "failure"})
    completed = run_ci(workflow, tmp_path, needs, "schedule")
    assert completed.returncode == 1
    assert "Jobs that did not succeed: audit failure, e2e skipped" in completed.stdout


@pytest.mark.parametrize(
    ("workflow_name", "event"),
    [
        ("CI", "pull_request"),
        ("CI", "push"),
        ("Release", "release"),
        ("Release", "workflow_dispatch"),
    ],
)
def test_ci_notes_the_e2e_suite_skipped_outside_the_runs_it_belongs_to(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path, workflow_name: str, event: str
) -> None:
    needs = results(needed, {"e2e": "skipped"})
    completed = run_ci(workflow, tmp_path, needs, event, workflow_name=workflow_name)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (
        f"::notice title=CI::e2e runs in CI's scheduled and manual runs only; skipped on this"
        f" {event} run."
    ) in completed.stdout
    assert "::warning" not in completed.stdout


@pytest.mark.parametrize("result", ["failure", "cancelled"])
def test_ci_still_warns_of_an_e2e_suite_that_ran_and_failed_elsewhere(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path, result: str
) -> None:
    completed = run_ci(workflow, tmp_path, results(needed, {"e2e": result}), "push")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"::warning title=CI::Advisory job did not succeed: e2e {result}" in completed.stdout
    assert "::notice" not in completed.stdout


@pytest.mark.parametrize("event", ["pull_request", "push", "schedule", "workflow_dispatch"])
def test_ci_passes_when_every_job_succeeded(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path, event: str
) -> None:
    completed = run_ci(workflow, tmp_path, results(needed), event)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"All {len(needed)} jobs passed." in completed.stdout


@pytest.mark.parametrize("event", ["pull_request", "push"])
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_ci_fails_when_a_job_did_not_succeed(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path, result: str, event: str
) -> None:
    completed = run_ci(workflow, tmp_path, results(needed, {"tests": result}), event)
    assert completed.returncode == 1
    assert f"::error title=CI::Jobs that did not succeed: tests {result}" in completed.stdout


def test_ci_names_every_job_that_did_not_succeed(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path
) -> None:
    needs = results(needed, {"audit": "failure", "gitleaks": "cancelled"})
    completed = run_ci(workflow, tmp_path, needs, "pull_request")
    assert completed.returncode == 1
    assert "Jobs that did not succeed: audit failure, gitleaks cancelled" in completed.stdout


def pull_request_jobs(workflow: dict[str, Any]) -> list[str]:
    jobs: str = find_step(workflow, CI_STEP)["env"]["PULL_REQUEST_JOBS"]
    return jobs.split()


def test_pull_request_jobs_are_the_needed_jobs_that_run_on_pull_requests_only(
    workflow: dict[str, Any], needed: list[str]
) -> None:
    on_pull_requests = [job for job in needed if workflow["jobs"][job].get("if") == PULL_REQUEST_IF]
    assert pull_request_jobs(workflow) == on_pull_requests == ["dependency-review", "mutation"]


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
@pytest.mark.parametrize("job", ["dependency-review", "mutation"])
def test_ci_accepts_a_pull_request_job_skipped_off_pull_requests(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path, event: str, job: str
) -> None:
    needs = results(needed, {job: "skipped"})
    completed = run_ci(workflow, tmp_path, needs, event)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (
        f"::notice title=CI::{job} runs on pull requests only; skipped on this {event} run."
    ) in completed.stdout


@pytest.mark.parametrize("event", ["push", "schedule"])
def test_ci_accepts_both_pull_request_jobs_skipped_off_pull_requests(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path, event: str
) -> None:
    needs = results(needed, {"dependency-review": "skipped", "mutation": "skipped"})
    completed = run_ci(workflow, tmp_path, needs, event)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.count("::notice title=CI::") == 2


@pytest.mark.parametrize("job", ["dependency-review", "mutation"])
def test_ci_fails_a_pull_request_job_skipped_on_a_pull_request(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path, job: str
) -> None:
    needs = results(needed, {job: "skipped"})
    completed = run_ci(workflow, tmp_path, needs, "pull_request")
    assert completed.returncode == 1
    assert f"Jobs that did not succeed: {job} skipped" in completed.stdout
    assert "::notice" not in completed.stdout


@pytest.mark.parametrize("result", ["failure", "cancelled"])
@pytest.mark.parametrize("job", ["dependency-review", "mutation"])
def test_ci_fails_a_pull_request_job_that_ran_and_failed_on_a_push(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path, result: str, job: str
) -> None:
    needs = results(needed, {job: result})
    completed = run_ci(workflow, tmp_path, needs, "push")
    assert completed.returncode == 1


def test_ci_fails_another_skipped_job_on_a_push(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path
) -> None:
    needs = results(needed, {"dependency-review": "skipped", "audit": "skipped"})
    completed = run_ci(workflow, tmp_path, needs, "push")
    assert completed.returncode == 1
    assert "Jobs that did not succeed: audit skipped" in completed.stdout


def test_ci_lets_an_advisory_job_fail_and_prints_its_result(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path
) -> None:
    needs = results(needed, {"tests": "failure"})
    completed = run_ci(workflow, tmp_path, needs, "pull_request", advisory="tests")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "::warning title=CI::Advisory job did not succeed: tests failure" in completed.stdout
    assert "tests failure" in completed.stdout.splitlines()


def test_ci_still_fails_other_jobs_beside_an_advisory_one(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path
) -> None:
    needs = results(needed, {"tests": "skipped", "package": "failure"})
    completed = run_ci(workflow, tmp_path, needs, "pull_request", advisory="tests")
    assert completed.returncode == 1
    assert "Jobs that did not succeed: package failure" in completed.stdout


def test_ci_says_nothing_of_an_advisory_job_that_succeeded(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path
) -> None:
    completed = run_ci(workflow, tmp_path, results(needed), "pull_request", advisory="tests")
    assert completed.returncode == 0
    assert "::warning" not in completed.stdout


def test_ci_exits_2_when_a_job_result_is_missing(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path
) -> None:
    completed = run_ci(workflow, tmp_path, results(needed[1:]), "pull_request")
    assert completed.returncode == 2
    assert f"{len(needed) - 1} job results, expected {len(needed)}" in completed.stdout


def test_ci_exits_2_on_an_extra_job_result(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path
) -> None:
    completed = run_ci(workflow, tmp_path, results([*needed, "extra"]), "pull_request")
    assert completed.returncode == 2


@pytest.mark.parametrize("raw", ["not json", "[1]"])
def test_ci_exits_2_when_the_results_cannot_be_read(
    workflow: dict[str, Any], tmp_path: Path, raw: str
) -> None:
    completed = run_ci(workflow, tmp_path, raw, "pull_request")
    assert completed.returncode == 2
    assert "Could not read the job results" in completed.stdout


@pytest.mark.parametrize("raw", ["", "{}"])
def test_ci_exits_2_when_no_job_result_arrives(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path, raw: str
) -> None:
    completed = run_ci(workflow, tmp_path, raw, "pull_request")
    assert completed.returncode == 2
    assert f"0 job results, expected {len(needed)}" in completed.stdout


def test_notify_reports_after_ci_on_scheduled_and_manual_runs_only(
    workflow: dict[str, Any], needed: list[str]
) -> None:
    notify = workflow["jobs"]["notify"]
    assert "notify" not in needed
    assert set(notify["needs"]) == {"audit", "ci", "e2e"}
    assert notify["if"] == (
        "always() && github.workflow == 'CI' &&"
        " (github.event_name == 'schedule' || github.event_name == 'workflow_dispatch')"
    )


@pytest.mark.parametrize(
    ("result", "line"),
    [
        ("success", ">e2e: success"),
        ("failure", ">e2e: failure (did not run, or a test failed)"),
        ("cancelled", ">e2e: cancelled (did not run to the end)"),
        ("skipped", ">e2e: skipped (did not run)"),
    ],
)
def test_notify_s_message_carries_the_e2e_result(
    workflow: dict[str, Any], tmp_path: Path, result: str, line: str
) -> None:
    step = find_step(workflow, ("notify", "Render the message"))
    assert step["env"]["E2E_RESULT"] == "${{ needs.e2e.result }}"
    stand_in(tmp_path / "bin", "uv", f'shift 5\nexec "{sys.executable}" "$@"')
    output = tmp_path / "output"
    env = {
        "AUDIT_DIR": str(tmp_path / "audit"),
        "GITHUB_OUTPUT": str(output),
        "REPO": "o/r",
        "RUN_URL": "https://example.invalid/run",
        "CI_RESULT": "success",
        "E2E_RESULT": result,
    }
    completed = run_step(workflow, ("notify", "Render the message"), tmp_path, env)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    text = output.read_text().splitlines()
    assert text[0].startswith("text<<EOF_")
    assert text[-1] == text[0].removeprefix("text<<")
    assert text[-4:-1] == [">CI: success", line, "><https://example.invalid/run|View the run>"]


# --- the needs check in the prek job -------------------------------------------


def run_needs_check(
    workflow: dict[str, Any], tmp_path: Path, planted: dict[str, Any] | None
) -> subprocess.CompletedProcess[str]:
    """Run the check against `planted` (JSON is YAML), or a missing file when it is None."""
    path = tmp_path / "planted.yml"
    if planted is not None:
        path.write_text(json.dumps(planted), encoding="utf-8")
    return run_step(workflow, NEEDS_CHECK_STEP, tmp_path, {"WORKFLOW": str(path)})


def plant(workflow: dict[str, Any], **ci_env: object) -> dict[str, Any]:
    planted = copy.deepcopy(workflow)
    find_step(planted, CI_STEP)["env"].update(ci_env)
    return planted


def set_needs(planted: dict[str, Any], needs: list[str]) -> None:
    planted["jobs"]["ci"]["needs"] = needs
    find_step(planted, CI_STEP)["env"]["EXPECTED_JOBS"] = len(needs)


def test_needs_check_passes_on_the_committed_workflow(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path
) -> None:
    completed = run_step(workflow, NEEDS_CHECK_STEP, tmp_path, {})
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"CI needs every job: {', '.join(sorted(needed))}. Advisory: e2e." in completed.stdout


def test_expected_jobs_is_the_number_of_needed_jobs(
    workflow: dict[str, Any], needed: list[str]
) -> None:
    assert find_step(workflow, CI_STEP)["env"]["EXPECTED_JOBS"] == len(needed)


def test_needs_check_fails_when_ci_does_not_need_a_job(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path
) -> None:
    planted = copy.deepcopy(workflow)
    set_needs(planted, [job for job in needed if job != "package"])
    completed = run_needs_check(workflow, tmp_path, planted)
    assert completed.returncode == 1
    assert "CI's needs must list every job" in completed.stdout
    assert "< package" in completed.stdout
    assert "EXPECTED_JOBS in the CI job" not in completed.stdout


def test_needs_check_fails_on_a_new_job_ci_does_not_need(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    planted = copy.deepcopy(workflow)
    planted["jobs"]["new-job"] = {"runs-on": "ubuntu-24.04", "steps": [{"run": "true"}]}
    completed = run_needs_check(workflow, tmp_path, planted)
    assert completed.returncode == 1
    assert "< new-job" in completed.stdout


def test_needs_check_fails_when_ci_needs_a_job_that_does_not_exist(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path
) -> None:
    planted = copy.deepcopy(workflow)
    set_needs(planted, [*needed, "no-such-job"])
    completed = run_needs_check(workflow, tmp_path, planted)
    assert completed.returncode == 1
    assert "> no-such-job" in completed.stdout


def test_needs_check_fails_when_ci_needs_a_job_twice(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path
) -> None:
    planted = copy.deepcopy(workflow)
    set_needs(planted, [*needed, "tests"])
    completed = run_needs_check(workflow, tmp_path, planted)
    assert completed.returncode == 1
    assert "CI needs tests more than once" in completed.stdout


def test_needs_check_does_not_ask_for_notify(workflow: dict[str, Any], tmp_path: Path) -> None:
    planted = copy.deepcopy(workflow)
    planted["jobs"]["notify"]["needs"] = ["ci"]
    completed = run_needs_check(workflow, tmp_path, planted)
    assert completed.returncode == 0, completed.stdout


def test_needs_check_accepts_an_advisory_job_ci_needs(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    completed = run_needs_check(workflow, tmp_path, plant(workflow, ADVISORY_JOBS="tests"))
    assert completed.returncode == 0, completed.stdout
    assert "Advisory: tests." in completed.stdout


def test_needs_check_fails_when_an_advisory_job_is_not_needed(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    completed = run_needs_check(workflow, tmp_path, plant(workflow, ADVISORY_JOBS="tests gone"))
    assert completed.returncode == 1
    assert "ADVISORY_JOBS lists gone, which CI does not need" in completed.stdout


def test_needs_check_fails_when_an_advisory_job_is_listed_twice(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    completed = run_needs_check(workflow, tmp_path, plant(workflow, ADVISORY_JOBS="tests tests"))
    assert completed.returncode == 1
    assert "ADVISORY_JOBS lists tests twice" in completed.stdout


@pytest.mark.parametrize("delta", [-1, 1])
def test_needs_check_fails_on_a_wrong_expected_jobs(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path, delta: int
) -> None:
    planted = plant(workflow, EXPECTED_JOBS=len(needed) + delta)
    completed = run_needs_check(workflow, tmp_path, planted)
    assert completed.returncode == 1
    assert (
        f"EXPECTED_JOBS in the CI job is {len(needed) + delta}, but CI needs {len(needed)} jobs"
    ) in completed.stdout


def test_needs_check_fails_when_the_ci_step_is_renamed(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path
) -> None:
    planted = copy.deepcopy(workflow)
    find_step(planted, CI_STEP)["name"] = "Renamed"
    completed = run_needs_check(workflow, tmp_path, planted)
    assert completed.returncode == 1
    assert f"EXPECTED_JOBS in the CI job is not set, but CI needs {len(needed)}" in (
        completed.stdout
    )


@pytest.mark.parametrize(
    "planted",
    [
        None,
        {},
        {"on": "push"},
        {"jobs": {"ci": {"steps": []}, "notify": {"steps": []}}},
    ],
    ids=["missing file", "empty", "no jobs", "only ci and notify"],
)
def test_needs_check_exits_2_when_no_job_is_read(
    workflow: dict[str, Any], tmp_path: Path, planted: dict[str, Any] | None
) -> None:
    completed = run_needs_check(workflow, tmp_path, planted)
    assert completed.returncode == 2
    assert "No jobs read from" in completed.stdout


# --- the hook count in the prek job --------------------------------------------


def run_hooks(
    workflow: dict[str, Any], tmp_path: Path, lines: list[str], exit_code: int = 0
) -> subprocess.CompletedProcess[str]:
    """Run the step with a stand-in `uv` that prints prek's report lines."""
    output = "\n".join(lines)
    stand_in(tmp_path / "bin", "uv", f"cat <<'OUT'\n{output}\nOUT\nexit {exit_code}")
    return run_step(workflow, HOOKS_STEP, tmp_path, {})


def expected_hooks(workflow: dict[str, Any]) -> int:
    return int(find_step(workflow, HOOKS_STEP)["env"]["EXPECTED_HOOKS"])


def test_hook_count_passes_when_every_expected_hook_passed(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    lines = [f"hook {index}....Passed" for index in range(expected_hooks(workflow))]
    completed = run_hooks(workflow, tmp_path, [*lines, "check json....(no files to check)Skipped"])
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_hook_count_exits_2_when_a_hook_is_skipped(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    lines = [f"hook {index}....Passed" for index in range(expected_hooks(workflow) - 1)]
    completed = run_hooks(workflow, tmp_path, [*lines, "ruff check....(no files to check)Skipped"])
    assert completed.returncode == 2
    assert f"hooks passed, expected {expected_hooks(workflow)}" in completed.stdout


def test_hook_count_exits_2_when_a_hook_is_added(workflow: dict[str, Any], tmp_path: Path) -> None:
    lines = [f"hook {index}....Passed" for index in range(expected_hooks(workflow) + 1)]
    assert run_hooks(workflow, tmp_path, lines).returncode == 2


def test_a_failing_hook_fails_the_step_with_prek_s_status(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    lines = [f"hook {index}....Passed" for index in range(expected_hooks(workflow))]
    completed = run_hooks(workflow, tmp_path, [*lines, "mypy....Failed"], exit_code=1)
    assert completed.returncode == 1


# --- the installed-versions check in the tests job -----------------------------


def run_versions_check(
    workflow: dict[str, Any], tmp_path: Path, resolved: str, freeze: str
) -> subprocess.CompletedProcess[str]:
    (tmp_path / "runtime.txt").write_text(resolved)
    stand_in(tmp_path / "bin", "uv", f"cat <<'OUT'\n{freeze}\nOUT")
    return run_step(workflow, VERSIONS_STEP, tmp_path, {"RESOLUTION": "lowest-direct"})


RESOLVED = "# via permit-mcp\naiohttp==3.14.3\n    # via permit-mcp\nmcp==2.2.0\n"


def test_versions_check_passes_when_the_floor_is_installed(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    freeze = "aiohttp==3.14.3\nmcp==2.2.0\npytest==9.1.1"
    completed = run_versions_check(workflow, tmp_path, RESOLVED, freeze)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "2 runtime packages at the lowest-direct resolution." in completed.stdout


def test_versions_check_fails_when_a_newer_version_is_installed(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    freeze = "aiohttp==3.14.3\nmcp==2.3.0"
    completed = run_versions_check(workflow, tmp_path, RESOLVED, freeze)
    assert completed.returncode == 1
    assert "Not installed as resolved: mcp==2.2.0." in completed.stdout


def test_versions_check_exits_2_on_an_empty_resolution(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    completed = run_versions_check(workflow, tmp_path, "# nothing\n", "mcp==2.2.0")
    assert completed.returncode == 2


# --- the install step in the tests job -----------------------------------------


def recording_uv(tmp_path: Path) -> Path:
    """A stand-in uv that records each call, one line of arguments per call."""
    log = tmp_path / "uv.log"
    stand_in(tmp_path / "bin", "uv", f'echo "$*" >>"{log}"')
    return log


@pytest.mark.parametrize("resolution", ["lowest-direct", "highest"])
def test_install_step_compiles_at_the_leg_s_resolution(
    workflow: dict[str, Any], tmp_path: Path, resolution: str
) -> None:
    log = recording_uv(tmp_path)
    completed = run_step(workflow, INSTALL_STEP, tmp_path, {"RESOLUTION": resolution})
    assert completed.returncode == 0, completed.stdout + completed.stderr
    compile_call, install_call = log.read_text().splitlines()
    assert compile_call.startswith("pip compile --quiet --no-sources --exclude-newer false")
    assert f"--resolution {resolution} pyproject.toml -o {tmp_path}/runtime.txt" in compile_call
    assert install_call == (
        f"pip install --no-sources --exclude-newer false . --group dev -c {tmp_path}/runtime.txt"
    )


# --- the audit gate step ---------------------------------------------------------


def plant_tree(directory: Path, tree: str, report: dict[str, Any], pinned: int) -> None:
    (directory / tree).mkdir(parents=True)
    pins = "".join(f"pkg{index}==1.0\n" for index in range(pinned))
    (directory / tree / "requirements.txt").write_text(pins)
    (directory / f"trivy-{tree}.json").write_text(json.dumps(report))


def test_audit_gate_step_maps_the_gate_s_exits(workflow: dict[str, Any], tmp_path: Path) -> None:
    package = [{"Name": "p", "Version": "1"}]
    clean = {"Results": [{"Target": "requirements.txt", "Packages": package}]}
    advisory = {"VulnerabilityID": "CVE-1", "PkgName": "p", "Severity": "HIGH", "FixedVersion": "2"}
    vulnerable = {"Results": [{**clean["Results"][0], "Vulnerabilities": [advisory]}]}
    for name, report in [("clean", clean), ("vulnerable", vulnerable)]:
        plant_tree(tmp_path / "audit", name, report, pinned=1)

    def gate(*trees: str) -> subprocess.CompletedProcess[str]:
        env = {"AUDIT_DIR": str(tmp_path / "audit"), "AUDIT_TREES": " ".join(trees)}
        return run_step(workflow, AUDIT_GATE_STEP, tmp_path, env)

    passed = gate("clean")
    assert passed.returncode == 0, passed.stdout + passed.stderr
    blocked = gate("clean", "vulnerable")
    assert blocked.returncode == 1
    assert "::error title=Dependency audit::Fixable HIGH or CRITICAL" in blocked.stdout
    incomplete = gate("clean", "absent")
    assert incomplete.returncode == 2
    assert "::error title=Dependency audit::The audit did not complete" in incomplete.stdout


def gate_with_a_vulnerable_tool_tree(
    workflow: dict[str, Any], tmp_path: Path, gate_dev_tree: str, tool_tree: str
) -> subprocess.CompletedProcess[str]:
    package = [{"Name": "p", "Version": "1"}]
    clean = {"Results": [{"Target": "requirements.txt", "Packages": package}]}
    advisory = {"VulnerabilityID": "CVE-1", "PkgName": "p", "Severity": "HIGH", "FixedVersion": "2"}
    vulnerable = {"Results": [{**clean["Results"][0], "Vulnerabilities": [advisory]}]}
    for tree in audit_trees(workflow):
        report = vulnerable if tree == tool_tree else clean
        plant_tree(tmp_path / "audit", tree, report, pinned=1)
    env = {"AUDIT_DIR": str(tmp_path / "audit"), "GATE_DEV_TREE": gate_dev_tree}
    return run_step(workflow, AUDIT_GATE_STEP, tmp_path, env)


@pytest.mark.parametrize("tool_tree", ["dev-ceiling", "docs-ceiling"])
@pytest.mark.parametrize("gate_dev_tree", ["true", "null"], ids=["true", "not called"])
def test_the_audit_gates_on_the_dev_and_docs_trees_by_default(
    workflow: dict[str, Any], tmp_path: Path, gate_dev_tree: str, tool_tree: str
) -> None:
    completed = gate_with_a_vulnerable_tool_tree(workflow, tmp_path, gate_dev_tree, tool_tree)
    assert completed.returncode == 1
    assert "::error title=Dependency audit::Fixable HIGH or CRITICAL" in completed.stdout


@pytest.mark.parametrize("tool_tree", ["dev-ceiling", "docs-ceiling"])
def test_a_caller_can_leave_the_dev_and_docs_trees_ungated(
    workflow: dict[str, Any], tmp_path: Path, tool_tree: str
) -> None:
    completed = gate_with_a_vulnerable_tool_tree(workflow, tmp_path, "false", tool_tree)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (
        "dev-ceiling docs-ceiling: reported in the summary but not gated (gate-dev-tree: false)."
    ) in completed.stdout


@pytest.mark.parametrize("tree", ["runtime-ceiling", "runtime-floor"])
def test_a_caller_that_ungates_the_tool_trees_still_gates_the_runtime_ones(
    workflow: dict[str, Any], tmp_path: Path, tree: str
) -> None:
    completed = gate_with_a_vulnerable_tool_tree(workflow, tmp_path, "false", tree)
    assert completed.returncode == 1


def test_gate_dev_tree_is_a_called_run_s_input_defaulting_to_true(
    workflow: dict[str, Any],
) -> None:
    assert workflow["on"]["workflow_call"]["inputs"]["gate-dev-tree"]["type"] == "boolean"
    assert workflow["on"]["workflow_call"]["inputs"]["gate-dev-tree"]["default"] is True
    gate = find_step(workflow, AUDIT_GATE_STEP)
    assert gate["env"] == {"GATE_DEV_TREE": "${{ toJSON(inputs.gate-dev-tree) }}"}
    for step in ("Write the job summary", "Annotate blocking advisories"):
        assert "GATE_DEV_TREE" not in find_step(workflow, ("audit", step)).get("env", {})


# --- audit-deps.sh -------------------------------------------------------------------


def audit_trees(workflow: dict[str, Any]) -> list[str]:
    value: str = workflow["env"]["AUDIT_TREES"]
    assert "\n" not in value, "AUDIT_TREES is read with `read -ra`, which reads one line"
    return value.split()


def test_audit_trees_are_the_trees_audit_deps_compiles(workflow: dict[str, Any]) -> None:
    compiled = re.findall(r"^compile_tree (\S+)", AUDIT_DEPS.read_text(), flags=re.MULTILINE)
    assert audit_trees(workflow) == compiled
    assert compiled == ["runtime-ceiling", "runtime-floor", "dev-ceiling", "docs-ceiling"]


def run_audit_deps_with_stand_ins(tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run audit-deps.sh with stand-ins for uv and trivy that record their calls.

    The stand-in compile writes a tree at the project's declared floors for
    `--resolution lowest-direct` and one above them otherwise; the stand-in `uv
    run` runs the real check_floors.py.
    """
    declared = floors(REPO_ROOT / "pyproject.toml")
    fillers = [f"filler{index}==1.0" for index in range(10)]
    trees = {
        "floor": [f"{name}=={version}" for name, version in declared.items()],
        "ceiling": [f"{name}==999.0" for name in declared],
    }
    trees["dev"] = [*trees["ceiling"], "pytest==9.1.1"]
    trees["docs"] = [*trees["ceiling"], "zensical==0.0.65"]
    for name, pins in trees.items():
        (tmp_path / f"{name}.txt").write_text("\n".join([*pins, *fillers]) + "\n")
    log = tmp_path / "calls.log"
    stand_in(
        tmp_path / "bin",
        "uv",
        f"""echo "uv $*" >>"{log}"
if [[ $1 == run ]]; then
  while [[ $1 != python ]]; do shift; done
  shift
  exec python3 "$@"
fi
tree={tmp_path}/ceiling.txt
while [[ $# -gt 0 ]]; do
  case $1 in
    -o) out=$2; shift ;;
    --resolution) [[ $2 == lowest-direct ]] && tree={tmp_path}/floor.txt; shift ;;
    --group) tree={tmp_path}/dev.txt; [[ $2 == *:docs ]] && tree={tmp_path}/docs.txt; shift ;;
  esac
  shift
done
cp "$tree" "$out\"""",
    )
    stand_in(
        tmp_path / "bin",
        "trivy",
        f"""echo "trivy $*" >>"{log}"
while [[ $1 != --output ]]; do shift; done
echo '{{"Results": []}}' >"$2\"""",
    )
    completed = subprocess.run(
        [tool("bash"), str(AUDIT_DEPS), str(tmp_path / "audit")],
        env={"PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}"},
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed, log


def test_audit_deps_compiles_each_tree_as_it_says(tmp_path: Path) -> None:
    completed, log = run_audit_deps_with_stand_ins(tmp_path)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    compiles = [line for line in log.read_text().splitlines() if line.startswith("uv pip compile")]
    assert len(compiles) == 4
    ceiling, floor, dev, docs = compiles
    for call in compiles:
        assert "--no-sources --exclude-newer false --python-version 3.11" in call
    assert "--resolution" not in ceiling
    assert "--group" not in ceiling
    assert "--resolution lowest-direct" in floor
    assert "--group" not in floor
    assert "--resolution" not in dev
    assert dev.endswith(
        "/pyproject.toml:dev --no-sources --exclude-newer false --python-version "
        "3.11 --quiet -o " + str(tmp_path / "audit" / "dev-ceiling" / "requirements.txt")
    )
    assert docs.endswith(
        "/pyproject.toml:docs --no-sources --exclude-newer false --python-version "
        "3.11 --quiet -o " + str(tmp_path / "audit" / "docs-ceiling" / "requirements.txt")
    )
    assert "Every direct dependency is at its floor" in completed.stdout


def test_audit_deps_scans_every_tree_with_no_repository_config(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    completed, log = run_audit_deps_with_stand_ins(tmp_path)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    scans = [line for line in log.read_text().splitlines() if line.startswith("trivy ")]
    expected = [
        f"trivy fs --config /dev/null --scanners vuln --severity UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL"
        f" --list-all-pkgs --format json --ignorefile /dev/null"
        f" --output {tmp_path}/audit/trivy-{tree}.json --quiet {tmp_path}/audit/{tree}"
        for tree in audit_trees(workflow)
    ]
    assert scans == expected


def test_audit_deps_fails_a_floor_tree_above_the_floors(tmp_path: Path) -> None:
    run_audit_deps_with_stand_ins(tmp_path)
    (tmp_path / "floor.txt").write_text((tmp_path / "ceiling.txt").read_text())
    completed = subprocess.run(
        [tool("bash"), str(AUDIT_DEPS), str(tmp_path / "audit2")],
        env={"PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}"},
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert "its floor is" in completed.stdout


@pytest.mark.parametrize(
    ("group", "marker"), [("dev", "pytest"), ("docs", "zensical")], ids=["dev", "docs"]
)
def test_audit_deps_fails_a_group_tree_without_the_group(
    tmp_path: Path, group: str, marker: str
) -> None:
    run_audit_deps_with_stand_ins(tmp_path)
    (tmp_path / f"{group}.txt").write_text((tmp_path / "ceiling.txt").read_text())
    completed = subprocess.run(
        [tool("bash"), str(AUDIT_DEPS), str(tmp_path / "audit2")],
        env={"PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}"},
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert f"Tree '{group}-ceiling' has no {marker}" in completed.stdout


def pinned_version(url_variable: str, workflow: dict[str, Any]) -> str:
    match = re.search(r"/download/v([0-9.]+)/", workflow["env"][url_variable])
    assert match is not None
    return match.group(1)


def test_a_planted_trivy_config_and_ignore_file_do_not_hide_advisories(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    # The pinned Trivy, on the real audit-deps.sh, in a planted repository whose
    # trivy.yaml asks for LOW only and whose .trivyignore lists the advisories.
    trivy = tool("trivy")
    version = subprocess.run([trivy, "--version"], capture_output=True, text=True, check=True)
    assert f"Version: {pinned_version('TRIVY_URL', workflow)}" in version.stdout
    repo = tmp_path / "repo"
    (repo / ".github" / "scripts").mkdir(parents=True)
    for script in ("audit-deps.sh", "check_floors.py", "format_audit.py"):
        shutil.copy(SCRIPTS / script, repo / ".github" / "scripts" / script)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "planted"\nversion = "0"\nrequires-python = ">=3.11"\n'
        'dependencies = ["aiohttp>=3.9.1,<3.9.2", "requests>=2.31.0,<2.31.1"]\n'
        '[dependency-groups]\ndev = ["pytest==9.1.1"]\ndocs = ["zensical==0.0.65"]\n'
    )
    (repo / "trivy.yaml").write_text("severity:\n  - LOW\n")
    (repo / ".trivyignore").write_text("CVE-2024-23334\nCVE-2024-30251\nCVE-2025-69223\n")
    out = repo / "audit"
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    completed = subprocess.run(
        [tool("bash"), str(repo / ".github" / "scripts" / "audit-deps.sh"), str(out)],
        env=env,
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    gate = subprocess.run(
        [
            sys.executable,
            SCRIPTS / "format_audit.py",
            "--dir",
            out,
            *audit_trees(workflow),
            "--gate",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert gate.returncode == 1, gate.stderr
    assert "HIGH CVE-2024-23334 aiohttp 3.9.1" in gate.stderr
    # Without --config and --ignorefile, the planted files hide every HIGH one.
    hidden = subprocess.run(
        [trivy, "fs", "--scanners", "vuln", "--format", "json", "--quiet", out / "runtime-floor"],
        env=env,
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    severities = {
        vuln["Severity"]
        for result in json.loads(hidden.stdout).get("Results") or []
        for vuln in result.get("Vulnerabilities") or []
    }
    assert severities <= {"LOW"}


# --- the history scan ------------------------------------------------------------

# What gitleaks 8.30.1 prints with --no-banner --no-color, its commit count read as 0
# as a git log format can make it.
CLEAN_SCAN = """8:14PM INF 0 commits scanned.
8:14PM INF scanned ~740369 bytes (740.37 KB) in 117ms
8:14PM INF no leaks found"""
LEAKY_SCAN = """Finding:     token = REDACTED
Secret:      REDACTED
RuleID:      github-pat
File:        token.txt
Line:        1

8:14PM INF 0 commits scanned.
8:14PM INF scanned ~119 bytes (119 bytes) in 83.1ms
8:14PM WRN leaks found: 1"""


def git_stand_in(tmp_path: Path, shallow: str, commits: str) -> None:
    """Answer (is shallow, commit count) as given, skip the mirror clone, pass the rest on."""
    stand_in(
        tmp_path / "bin",
        "git",
        f'case "$1 $2" in\n'
        f'  "rev-parse --is-shallow-repository") echo {shallow} ;;\n'
        f"  clone*) ;;\n"
        f'  *) if [[ " $* " == *" rev-list "* ]]; then echo {commits};'
        f' else exec "{tool("git")}" "$@"; fi ;;\n'
        f"esac",
    )


def run_scan(
    workflow: dict[str, Any],
    tmp_path: Path,
    *,
    output: str,
    exit_code: int,
    git_answers: tuple[str, str] = ("false", "14"),
) -> subprocess.CompletedProcess[str]:
    """Run the step on a push with stand-ins for gitleaks, and for git (is shallow, commits)."""
    git_stand_in(tmp_path, *git_answers)
    stand_in(tmp_path / "bin", "gitleaks", f"cat >&2 <<'OUT'\n{output}\nOUT\nexit {exit_code}")
    return run_step(workflow, GITLEAKS_STEP, tmp_path, {"EVENT": "push"})


def test_scan_passes_a_clean_history(workflow: dict[str, Any], tmp_path: Path) -> None:
    completed = run_scan(workflow, tmp_path, output=CLEAN_SCAN, exit_code=0)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "scanned 740369 bytes of 14 commits and found no secret" in completed.stdout


def test_scan_reads_no_configuration_from_the_checkout(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    completed = run_scan(workflow, tmp_path, output=CLEAN_SCAN, exit_code=0)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    recording = f'printf "%s\\n" "$@" >"{tmp_path}/args"\ncat >&2 <<\'OUT\'\n{CLEAN_SCAN}\nOUT'
    stand_in(tmp_path / "bin", "gitleaks", recording)
    assert run_step(workflow, GITLEAKS_STEP, tmp_path, {"EVENT": "push"}).returncode == 0
    args = (tmp_path / "args").read_text().splitlines()
    assert args[0] == "git"
    assert args[args.index("--config") + 1] == f"{tmp_path}/gitleaks.toml"
    assert (tmp_path / "gitleaks.toml").read_text() == "[extend]\nuseDefault = true\n"
    ignore_path = Path(args[args.index("--gitleaks-ignore-path") + 1])
    assert ignore_path.is_dir()
    assert [entry.name for entry in ignore_path.iterdir()] == [".gitleaksignore"]
    accepted = [
        line.split()[0]
        for line in (REPO_ROOT / ".github" / "gitleaks-accept.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert (ignore_path / ".gitleaksignore").read_text().split() == accepted
    assert {"--ignore-gitleaks-allow", "--redact", "--exit-code"} <= set(args)
    assert args[-1] == f"{tmp_path}/history.git", "the scan reads the mirror, not the checkout"


def test_scan_fails_a_planted_leak(workflow: dict[str, Any], tmp_path: Path) -> None:
    completed = run_scan(workflow, tmp_path, output=LEAKY_SCAN, exit_code=1)
    assert completed.returncode == 1
    assert "::error title=gitleaks::Secrets found in the history" in completed.stdout


@pytest.mark.parametrize(
    "output",
    [CLEAN_SCAN.replace("~740369", "~0"), "8:14PM FTL could not run git log"],
    ids=["no bytes", "no count"],
)
def test_scan_exits_2_when_nothing_was_scanned(
    workflow: dict[str, Any], tmp_path: Path, output: str
) -> None:
    completed = run_scan(workflow, tmp_path, output=output, exit_code=1)
    assert completed.returncode == 2
    assert "gitleaks scanned nothing" in completed.stdout


def test_scan_exits_2_on_a_shallow_checkout(workflow: dict[str, Any], tmp_path: Path) -> None:
    completed = run_scan(
        workflow, tmp_path, output=CLEAN_SCAN, exit_code=0, git_answers=("true", "14")
    )
    assert completed.returncode == 2
    assert "The checkout is shallow" in completed.stdout


def test_scan_exits_2_on_an_empty_history(workflow: dict[str, Any], tmp_path: Path) -> None:
    completed = run_scan(
        workflow, tmp_path, output=CLEAN_SCAN, exit_code=0, git_answers=("false", "0")
    )
    assert completed.returncode == 2
    assert "holds no commit" in completed.stdout


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        [tool("git"), "-C", str(repo), *args],
        env={**GIT_ENV, "PATH": os.environ["PATH"]},
        check=True,
        capture_output=True,
    )


GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "planted",
    "GIT_AUTHOR_EMAIL": "planted@example.invalid",
    "GIT_COMMITTER_NAME": "planted",
    "GIT_COMMITTER_EMAIL": "planted@example.invalid",
}


def planted_repo(tmp_path: Path, *, token: bool, suppressions: bool) -> Path:
    """A repository with a GitHub token committed, and the suppressions a PR could add."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--quiet")
    (repo / "README.md").write_text("planted\n")
    if token:
        alphabet = string.ascii_letters + string.digits
        value = "ghp" + "_" + "".join(secrets.choice(alphabet) for _ in range(36))
        (repo / "token.txt").write_text(f"token = {value}  # gitleaks:allow\n")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "plant")
    if suppressions:
        (repo / ".gitleaks.toml").write_text(
            "[extend]\nuseDefault = true\n\n[allowlist]\npaths = ['''token\\.txt''']\n"
        )
        head = subprocess.run(
            [tool("git"), "-C", str(repo), "rev-parse", "HEAD"],
            env={**GIT_ENV, "PATH": os.environ["PATH"]},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        (repo / ".gitleaksignore").write_text(f"{head}:token.txt:github-pat:1\n")
        git(repo, "add", ".")
        git(repo, "commit", "--quiet", "-m", "suppress")
    return repo


def run_real_scan(
    workflow: dict[str, Any], tmp_path: Path, repo: Path, event: str = "push"
) -> subprocess.CompletedProcess[str]:
    """Run the step on `event` with the pinned gitleaks in the planted repository."""
    gitleaks = tool("gitleaks")
    version = subprocess.run([gitleaks, "version"], capture_output=True, text=True, check=True)
    assert version.stdout.strip() == pinned_version("GITLEAKS_URL", workflow)
    (tmp_path / "bin").mkdir(exist_ok=True)
    shutil.copy(gitleaks, tmp_path / "bin" / "gitleaks")
    env = {**GIT_ENV, "HOME": os.environ["HOME"], "EVENT": event}
    return run_step(workflow, GITLEAKS_STEP, tmp_path, env, cwd=repo)


def test_the_pinned_gitleaks_passes_a_clean_repository(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    repo = planted_repo(tmp_path, token=False, suppressions=False)
    completed = run_real_scan(workflow, tmp_path, repo)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "of 1 commits and found no secret" in completed.stdout


def test_the_pinned_gitleaks_finds_a_planted_token_despite_the_repository_s_suppressions(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    repo = planted_repo(tmp_path, token=True, suppressions=True)
    # The suppressions work when gitleaks reads them from the repository...
    default = subprocess.run(
        [tool("gitleaks"), "git", "--no-banner", "--exit-code", "1", "."],
        env={**GIT_ENV, "PATH": os.environ["PATH"], "HOME": os.environ["HOME"]},
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert default.returncode == 0, default.stdout + default.stderr
    # ...and the step reads none of them.
    completed = run_real_scan(workflow, tmp_path, repo)
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "RuleID:      github-pat" in completed.stdout
    assert "ghp_" not in completed.stdout, "findings are redacted"


ACCEPT_FILE = Path(".github") / "gitleaks-accept.txt"


def commit_accept_list(repo: Path, text: str | None, message: str) -> None:
    """Commit `text` as the accept list, or commit without one when it is None."""
    if text is None:
        (repo / "other.txt").write_text(message)
    else:
        (repo / ".github").mkdir(exist_ok=True)
        (repo / ACCEPT_FILE).write_text(text)
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", message)


def checkout_with_accept_list(tmp_path: Path, text: str) -> Path:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git(checkout, "init", "--quiet")
    commit_accept_list(checkout, text, "accept")
    return checkout


def merged_pull_request(tmp_path: Path, base: str | None, pull_request: str | None) -> Path:
    """A checkout of a pull request's merge commit, as actions/checkout makes on pull_request.

    `base` and `pull_request` are the accept list on each side; None commits none.
    """
    repo = tmp_path / "checkout"
    repo.mkdir()
    git(repo, "init", "--quiet", "--initial-branch=main")
    (repo / "README.md").write_text("planted\n")
    commit_accept_list(repo, base, "base")
    git(repo, "checkout", "--quiet", "-b", "pr")
    commit_accept_list(repo, pull_request, "pull request")
    git(repo, "checkout", "--quiet", "main")
    git(repo, "merge", "--quiet", "--no-ff", "-m", "merge", "pr")
    return repo


def accepted_in_scan(
    workflow: dict[str, Any], tmp_path: Path, repo: Path, event: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the step with real git and a clean stand-in gitleaks; return what it accepted."""
    stand_in(tmp_path / "bin", "gitleaks", f"cat >&2 <<'OUT'\n{CLEAN_SCAN}\nOUT")
    env = {**GIT_ENV, "EVENT": event}
    completed = run_step(workflow, GITLEAKS_STEP, tmp_path, env, cwd=repo)
    ignored = (tmp_path / "gitleaks-ignores" / ".gitleaksignore").read_text().split()
    return completed, ignored


FINGERPRINT = "0" * 40 + ":tests/x.py:generic-api-key:3"
BASE_ENTRY = "a" * 40 + ":base.txt:github-pat:1"
PULL_REQUEST_ENTRY = "b" * 40 + ":pr.txt:github-pat:1"


def test_a_pull_request_reads_the_accept_list_from_its_base(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    base = f"{BASE_ENTRY}  # reviewed on main\n"
    repo = merged_pull_request(tmp_path, base, f"{base}{PULL_REQUEST_ENTRY}  # mine\n")
    completed, ignored = accepted_in_scan(workflow, tmp_path, repo, "pull_request")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert ignored == [BASE_ENTRY]
    assert f"Accepted as a false positive: {BASE_ENTRY}" in completed.stdout
    assert PULL_REQUEST_ENTRY not in completed.stdout
    assert "Reading .github/gitleaks-accept.txt from HEAD^1." in completed.stdout
    assert "::warning" not in completed.stdout


def test_a_pull_request_that_deletes_the_list_still_gets_the_base_s(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    repo = merged_pull_request(tmp_path, f"{BASE_ENTRY}  # reviewed on main\n", None)
    git(repo, "rm", "--quiet", str(ACCEPT_FILE))
    git(repo, "commit", "--quiet", "--amend", "-m", "merge without the list")
    completed, ignored = accepted_in_scan(workflow, tmp_path, repo, "pull_request")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert ignored == [BASE_ENTRY]
    assert "::warning" not in completed.stdout


def test_a_pull_request_reads_its_own_list_when_the_base_has_none(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    repo = merged_pull_request(tmp_path, None, f"{PULL_REQUEST_ENTRY}  # the first list\n")
    completed, ignored = accepted_in_scan(workflow, tmp_path, repo, "pull_request")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert ignored == [PULL_REQUEST_ENTRY]
    assert (
        "::warning title=gitleaks::The base (HEAD^1) has no .github/gitleaks-accept.txt, so the"
        " pull request's own list is read. Review every entry in it."
    ) in completed.stdout
    assert "Reading .github/gitleaks-accept.txt from HEAD." in completed.stdout


def test_a_pull_request_without_a_list_on_either_side_accepts_nothing(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    repo = merged_pull_request(tmp_path, None, None)
    completed, ignored = accepted_in_scan(workflow, tmp_path, repo, "pull_request")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert ignored == []
    assert "::warning" not in completed.stdout
    assert "::notice" not in completed.stdout


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
def test_other_events_read_the_accept_list_from_head(
    workflow: dict[str, Any], tmp_path: Path, event: str
) -> None:
    base = f"{BASE_ENTRY}  # reviewed on main\n"
    repo = merged_pull_request(tmp_path, base, f"{base}{PULL_REQUEST_ENTRY}  # merged\n")
    (repo / ACCEPT_FILE).write_text("not committed, so not read\n")
    completed, ignored = accepted_in_scan(workflow, tmp_path, repo, event)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert ignored == [BASE_ENTRY, PULL_REQUEST_ENTRY]
    assert "::warning" not in completed.stdout


def test_a_pull_request_whose_head_has_no_parent_exits_2(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    checkout = checkout_with_accept_list(tmp_path, f"{PULL_REQUEST_ENTRY}  # mine\n")
    completed, ignored = accepted_in_scan(workflow, tmp_path, checkout, "pull_request")
    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert "::error title=gitleaks::HEAD has no parent" in completed.stdout
    assert ignored == []


@pytest.mark.parametrize(
    "line",
    [
        f"{FINGERPRINT}",
        f"{FINGERPRINT}  #",
        "tests/x.py:generic-api-key:3  # no commit",
        "*  # all",
    ],
    ids=["no reason", "empty reason", "no commit", "wildcard"],
)
def test_an_accept_line_without_a_fingerprint_and_a_reason_exits_2(
    workflow: dict[str, Any], tmp_path: Path, line: str
) -> None:
    checkout = checkout_with_accept_list(tmp_path, f"# comment\n\n{line}\n")
    completed, _ = accepted_in_scan(workflow, tmp_path, checkout, "push")
    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert (
        "::error title=gitleaks::HEAD:.github/gitleaks-accept.txt:3 is not a fingerprint"
        " and a reason."
    ) in completed.stdout


def commit_token(repo: Path, name: str) -> str:
    """Commit a planted GitHub token in `name`; return its gitleaks fingerprint."""
    alphabet = string.ascii_letters + string.digits
    value = "ghp" + "_" + "".join(secrets.choice(alphabet) for _ in range(36))
    (repo / name).write_text(f"token = {value}\n")
    git(repo, "add", name)
    git(repo, "commit", "--quiet", "-m", f"plant {name}")
    head = subprocess.run(
        [tool("git"), "-C", str(repo), "rev-parse", "HEAD"],
        env={**GIT_ENV, "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return f"{head}:{name}:github-pat:1"


def test_an_accepted_fingerprint_hides_only_its_own_finding(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    repo = planted_repo(tmp_path, token=False, suppressions=False)
    first = commit_token(repo, "first.txt")
    (repo / ".github").mkdir()
    (repo / ".github" / "gitleaks-accept.txt").write_text(f"{first}  # planted for the test\n")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "accept first")
    accepted_only = run_real_scan(workflow, tmp_path, repo)
    assert accepted_only.returncode == 0, accepted_only.stdout + accepted_only.stderr
    assert f"Accepted as a false positive: {first}" in accepted_only.stdout

    commit_token(repo, "second.txt")
    second_run = tmp_path / "second-run"
    second_run.mkdir()
    completed = run_real_scan(workflow, second_run, repo)
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "File:        second.txt" in completed.stdout
    assert "File:        first.txt" not in completed.stdout


def test_the_pinned_gitleaks_finds_a_token_a_pull_request_accepts_for_itself(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    git(repo, "init", "--quiet", "--initial-branch=main")
    token_a = commit_token(repo, "a.txt")
    commit_accept_list(repo, f"{token_a}  # reviewed on main\n", "accept a")
    git(repo, "checkout", "--quiet", "-b", "pr")
    token_b = commit_token(repo, "b.txt")
    commit_accept_list(repo, f"{token_a}  # reviewed on main\n{token_b}  # mine\n", "accept b")
    git(repo, "checkout", "--quiet", "main")
    git(repo, "merge", "--quiet", "--no-ff", "-m", "merge", "pr")

    for run_dir in ("pull-request", "push"):
        (tmp_path / run_dir).mkdir()
    completed = run_real_scan(workflow, tmp_path / "pull-request", repo, "pull_request")
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "File:        b.txt" in completed.stdout
    assert "File:        a.txt" not in completed.stdout
    assert f"Accepted as a false positive: {token_a}" in completed.stdout
    assert f"Accepted as a false positive: {token_b}" not in completed.stdout

    # Once merged, the entry is the base's and counts.
    merged = run_real_scan(workflow, tmp_path / "push", repo, "push")
    assert merged.returncode == 0, merged.stdout + merged.stderr


# --- the API coverage report ----------------------------------------------------------

# github.workflow is the caller's name in a called run, so a release's dry run
# (a workflow_dispatch of Release) never reads the live spec.
LIVE = (
    "github.workflow == 'CI' &&"
    " (github.event_name == 'schedule' || github.event_name == 'workflow_dispatch')"
)
SNAPSHOT_STEP = ("tests", "Snapshot the live control-plane spec")
COVERAGE_STEP = ("tests", "Report API coverage")
COMMITTED_INVENTORY = REPO_ROOT / ".github" / "api-specs" / "control-plane.json"


def test_the_offline_tests_record_where_the_report_reads(workflow: dict[str, Any]) -> None:
    offline = find_step(workflow, ("tests", "Offline tests"))
    assert offline["env"] == {
        "PERMIT_MCP_API_RECORD": (
            "${{ matrix.resolution == 'locked' && "
            "format('{0}/api-record.jsonl', runner.temp) || '' }}"
        )
    }
    report = find_step(workflow, COVERAGE_STEP)["run"]
    assert '--record "$RUNNER_TEMP/api-record.jsonl"' in report
    assert '--origins "$RUNNER_TEMP/api-record.origins.json"' in report
    steps = [step_key(step) for step in workflow["jobs"]["tests"]["steps"]]
    assert steps.index("Offline tests") < steps.index(SNAPSHOT_STEP[1])
    assert steps.index(SNAPSHOT_STEP[1]) < steps.index(COVERAGE_STEP[1])
    assert steps.index(COVERAGE_STEP[1]) < steps.index("actions/upload-artifact")


def plant_coverage_record(runner_temp: Path) -> None:
    from test_api_coverage import ORIGINS, wire_record  # noqa: PLC0415 - one planted record

    lines = "".join(json.dumps(line) + "\n" for line in wire_record())
    (runner_temp / "api-record.jsonl").write_text(lines, encoding="utf-8")
    origins = runner_temp / "api-record.origins.json"
    origins.write_text(json.dumps(ORIGINS), encoding="utf-8")


def test_a_pull_request_reports_against_the_committed_inventory(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    plant_coverage_record(tmp_path)
    summary = tmp_path / "summary.md"
    completed = run_step(
        workflow,
        COVERAGE_STEP,
        tmp_path,
        {"EVENT": "pull_request", "WORKFLOW": "CI", "GITHUB_STEP_SUMMARY": str(summary)},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    text = summary.read_text(encoding="utf-8")
    assert "| Control plane | 23 | 12 | 0 | 11 |" in text
    assert "taken from https://api.permit.io/v2/openapi.json" in text
    assert "baseline" not in text


def live_spec(tmp_path: Path, *, drift: bool) -> Path:
    """An OpenAPI document of the committed inventory and 200 out-of-scope operations.

    With `drift`, list_access_requests's resource_instance_id query parameter is renamed.
    """
    from test_api_coverage import as_openapi  # noqa: PLC0415 - one planted spec

    document = as_openapi(COMMITTED_INVENTORY, filler=200)
    if drift:
        path = (
            "/v2/facts/{proj_id}/{env_id}/access_requests/{elements_config_id}/user/{user_id}"
            "/tenant/{tenant_id}"
        )
        for parameter in document["paths"][path]["get"]["parameters"]:
            if parameter["name"] == "resource_instance_id":
                parameter["name"] = "resource_instance"
    spec = tmp_path / "planted-openapi.json"
    spec.write_text(json.dumps(document), encoding="utf-8")
    return spec


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
@pytest.mark.parametrize("drift", [False, True], ids=["unchanged", "drifted"])
def test_scheduled_and_manual_runs_check_the_live_spec_for_drift(
    workflow: dict[str, Any], tmp_path: Path, event: str, *, drift: bool
) -> None:
    spec = live_spec(tmp_path, drift=drift)
    stand_in(
        tmp_path / "bin",
        "curl",
        'echo "$*" >"$RUNNER_TEMP/curl.txt"\n'
        "while [[ $# -gt 0 ]]; do [[ $1 == --output ]] && out=$2; shift; done\n"
        f'cp "{spec}" "$out"',
    )
    snapshot = run_step(workflow, SNAPSHOT_STEP, tmp_path, {})
    assert snapshot.returncode == 0, snapshot.stdout + snapshot.stderr
    assert "--fail" in (tmp_path / "curl.txt").read_text(encoding="utf-8")
    assert "https://api.permit.io/v2/openapi.json" in (tmp_path / "curl.txt").read_text()
    live = tmp_path / "api-specs" / "control-plane.json"
    assert live.exists()
    if not drift:
        assert live.read_text(encoding="utf-8") == COMMITTED_INVENTORY.read_text(encoding="utf-8")

    plant_coverage_record(tmp_path)
    summary = tmp_path / "summary.md"
    env = {"EVENT": event, "WORKFLOW": "CI", "GITHUB_STEP_SUMMARY": str(summary)}
    completed = run_step(workflow, COVERAGE_STEP, tmp_path, env)
    text = summary.read_text(encoding="utf-8")
    assert f"- Control plane: `{live}`" in text
    assert "- Control plane baseline: `.github/api-specs/control-plane.json`" in text
    if drift:
        assert completed.returncode == 1
        assert "'query resource_instance_id'" in text
        assert "'query resource_instance'" in text
    else:
        assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize("event", ["release", "workflow_dispatch"])
def test_a_run_release_yml_calls_reports_against_the_committed_inventory(
    workflow: dict[str, Any], tmp_path: Path, event: str
) -> None:
    plant_coverage_record(tmp_path)
    summary = tmp_path / "summary.md"
    env = {"EVENT": event, "WORKFLOW": "Release", "GITHUB_STEP_SUMMARY": str(summary)}
    completed = run_step(workflow, COVERAGE_STEP, tmp_path, env)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    text = summary.read_text(encoding="utf-8")
    assert "- Control plane: `.github/api-specs/control-plane.json`" in text
    assert "baseline" not in text


def test_the_live_spec_steps_run_in_ci_itself_only(workflow: dict[str, Any]) -> None:
    snapshot = " ".join(find_step(workflow, SNAPSHOT_STEP)["if"].split())
    assert snapshot == f"matrix.resolution == 'locked' && {LIVE}"
    assert find_step(workflow, COVERAGE_STEP)["env"]["WORKFLOW"] == "${{ github.workflow }}"
    assert workflow["name"] == "CI"


def test_a_failed_spec_download_fails_the_snapshot_step(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    stand_in(tmp_path / "bin", "curl", "exit 22")
    completed = run_step(workflow, SNAPSHOT_STEP, tmp_path, {})
    assert completed.returncode == 22
    assert not (tmp_path / "api-specs").exists()


# --- the surface report in the tests job ------------------------------------------

SNAPSHOT = REPO_ROOT / "tests" / "snapshots" / "surface.json"


def python_uv(tmp_path: Path) -> None:
    """A stand-in uv whose `uv run ... python ARGS` runs this interpreter on ARGS."""
    stand_in(
        tmp_path / "bin",
        "uv",
        f'while [[ $1 != python ]]; do shift; done\nshift\nexec "{sys.executable}" "$@"',
    )


def write_snapshot(repo: Path, snapshot: object | None) -> None:
    path = repo / "tests" / "snapshots" / "surface.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if snapshot is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(json.dumps(snapshot))


def surface_repos(tmp_path: Path, base: object | None, *, later_base: object = None) -> Path:
    """A checkout of a pull request's merge commit, as actions/checkout makes on one.

    main holds `base` as its snapshot (None: none), and the pull request branch the
    committed snapshot. HEAD merges the branch into main, so HEAD^1 is main as it was
    merged. With `later_base`, main then moves on to that snapshot, past the merge.
    """
    checkout = tmp_path / "checkout"
    (checkout / ".github" / "scripts").mkdir(parents=True)
    git(checkout, "init", "--quiet", "--initial-branch=main")
    shutil.copy(SCRIPTS / "surface_diff.py", checkout / ".github" / "scripts")
    write_snapshot(checkout, base)
    git(checkout, "add", ".")
    git(checkout, "commit", "--quiet", "-m", "base")
    git(checkout, "switch", "--quiet", "-c", "pull-request")
    write_snapshot(checkout, committed_snapshot())
    git(checkout, "add", ".")
    git(checkout, "commit", "--quiet", "--allow-empty", "-m", "change")
    git(checkout, "switch", "--quiet", "main")
    if later_base is not None:
        git(checkout, "switch", "--quiet", "-c", "later")
        write_snapshot(checkout, later_base)
        git(checkout, "add", ".")
        git(checkout, "commit", "--quiet", "-m", "main moves on")
        git(checkout, "switch", "--quiet", "main")
    git(checkout, "merge", "--quiet", "--no-ff", "-m", "merge", "pull-request")
    return checkout


def run_surface_report(
    workflow: dict[str, Any], tmp_path: Path, base: object | None, *, later_base: object = None
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run the step in a planted checkout of a merge commit."""
    checkout = surface_repos(tmp_path, base, later_base=later_base)
    python_uv(tmp_path)
    summary = tmp_path / "summary.md"
    env = {**GIT_ENV, "BASE_REF": "main", "GITHUB_STEP_SUMMARY": str(summary)}
    completed = run_step(workflow, SURFACE_STEP, tmp_path, env, cwd=checkout)
    return completed, summary.read_text() if summary.exists() else ""


def committed_snapshot() -> dict[str, Any]:
    snapshot: dict[str, Any] = json.loads(SNAPSHOT.read_text())
    return snapshot


def test_surface_report_lists_no_change_against_the_same_snapshot(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    completed, summary = run_surface_report(workflow, tmp_path, committed_snapshot())
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "surface_diff: 0 changes, 0 breaking" in completed.stdout
    assert "::warning" not in completed.stdout
    assert summary.startswith("### Tool surface changes against main (HEAD^1)\n```\n")


def test_surface_report_lists_a_non_breaking_change_without_a_warning(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    base = committed_snapshot()
    base["tools"]["check_permission"]["description"] = "Older wording."
    completed, summary = run_surface_report(workflow, tmp_path, base)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "non-breaking  tool check_permission: description changed" in summary
    assert "::warning" not in completed.stdout


def test_surface_report_warns_on_a_breaking_change_and_passes(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    base = committed_snapshot()
    base["tools"]["retired_tool"] = base["tools"]["check_permission"]
    completed, summary = run_surface_report(workflow, tmp_path, base)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "BREAKING      tool retired_tool: removed" in summary
    assert "::warning title=Breaking surface changes::See the job summary." in completed.stdout


def test_surface_report_passes_when_the_base_has_no_snapshot(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    completed, summary = run_surface_report(workflow, tmp_path, None)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "No base snapshot: main (HEAD^1) has no tests/snapshots/surface.json" in summary


def test_surface_report_fails_when_it_cannot_compare(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    completed, _ = run_surface_report(workflow, tmp_path, {"not": "a snapshot"})
    assert completed.returncode == 2
    assert "::error title=Surface diff::Could not compare the snapshots." in completed.stdout


def test_surface_report_compares_with_the_merged_base_not_the_moving_tip(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    # main gained a tool after this merge commit was made; the pull request did not
    # remove it, so nothing is breaking.
    later = committed_snapshot()
    later["tools"]["added_later"] = later["tools"]["check_permission"]
    completed, summary = run_surface_report(
        workflow, tmp_path, committed_snapshot(), later_base=later
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "surface_diff: 0 changes, 0 breaking" in summary
    assert "added_later" not in summary


def test_the_tests_job_fetches_the_merge_commit_s_base(workflow: dict[str, Any]) -> None:
    checkout = workflow["jobs"]["tests"]["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"] == {"fetch-depth": 2, "persist-credentials": False}
    run = find_step(workflow, SURFACE_STEP)["run"]
    assert "git fetch" not in run
    assert 'git show "HEAD^1:$SNAPSHOT"' in run


# --- the mutation job ------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "annotation"),
    [
        (0, None),
        (1, "::error title=Mutation tests::The tests catch too few mutants"),
        (2, "::error title=Mutation tests::The mutation tests did not run"),
    ],
)
def test_mutation_step_maps_the_gate_s_exits(
    workflow: dict[str, Any], tmp_path: Path, status: int, annotation: str | None
) -> None:
    log = tmp_path / "uv.log"
    stand_in(tmp_path / "bin", "uv", f'echo "$*" >>"{log}"\nexit {status}')
    completed = run_step(workflow, MUTATION_STEP, tmp_path, {})
    assert completed.returncode == status
    assert log.read_text() == (
        "run --locked python .github/scripts/mutation_gate.py --base HEAD^1 --threshold 80"
        " --workers 4 --memory-mib 3072 --max-minutes 25\n"
    )
    if annotation is None:
        assert "::error" not in completed.stdout
    else:
        assert annotation in completed.stdout


def test_the_mutation_job_diffs_the_merge_commit_against_its_base(
    workflow: dict[str, Any],
) -> None:
    job = workflow["jobs"]["mutation"]
    checkout, setup_uv, install, gate = job["steps"]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"] == {"fetch-depth": 2, "persist-credentials": False}
    assert setup_uv["uses"].startswith("astral-sh/setup-uv@")
    assert setup_uv["with"]["python-version"] == "3.13"
    assert install == {"name": "Install from uv.lock", "run": "uv sync --locked"}
    assert gate["name"] == MUTATION_STEP[1]
    assert job["timeout-minutes"] == 30
    assert "strategy" not in job


# --- the workflow's gates are wired as they say ---------------------------------

GATING_STEP_IFS = {
    ("tests", "Install from the ranges in pyproject.toml"): "matrix.resolution != 'locked'",
    ("tests", "Check that the runtime dependencies are at their floors"): (
        "matrix.resolution == 'lowest-direct'"
    ),
    ("tests", "Check the installed runtime versions"): "matrix.resolution != 'locked'",
    ("tests", "Install from uv.lock"): "matrix.resolution == 'locked'",
    ("tests", "Snapshot the live control-plane spec"): f"matrix.resolution == 'locked' && {LIVE}",
    ("tests", "Report API coverage"): "matrix.resolution == 'locked'",
    ("tests", "actions/upload-artifact"): (
        f"${{{{ !cancelled() && matrix.resolution == 'locked' && {LIVE} }}}}"
    ),
    ("audit", "Write the job summary"): "${{ !cancelled() }}",
    ("audit", "Annotate blocking advisories"): "${{ !cancelled() }}",
    ("audit", "Gate on fixable HIGH and CRITICAL advisories"): "${{ !cancelled() }}",
    ("audit", "actions/upload-artifact"): "${{ !cancelled() }}",
    SURFACE_STEP: "matrix.resolution == 'locked' && github.event_name == 'pull_request'",
}
GATING_JOB_IFS = {
    "ci": "always()",
    "dependency-review": PULL_REQUEST_IF,
    "mutation": PULL_REQUEST_IF,
    "e2e": SCHEDULED_IF,
}
RUN_ID_GROUP = (
    "(github.event_name == 'schedule' || github.event_name == 'workflow_dispatch')"
    " && github.run_id || 'shared' }}"
)


def step_key(step: dict[str, Any]) -> str:
    return str(step.get("name") or step["uses"].split("@")[0])


def test_only_notify_s_artifact_download_may_fail(workflow: dict[str, Any]) -> None:
    allowed = []
    for job_name, job in workflow["jobs"].items():
        assert "continue-on-error" not in job, job_name
        allowed += [
            (job_name, step_key(step)) for step in job["steps"] if "continue-on-error" in step
        ]
    assert allowed == [("notify", "actions/download-artifact")]


def test_every_gating_step_runs_when_it_says(workflow: dict[str, Any], needed: list[str]) -> None:
    found = {}
    for job_name in needed:
        job = workflow["jobs"][job_name]
        if "if" in job:
            assert GATING_JOB_IFS.get(job_name) == job["if"], job_name
        for step in job["steps"]:
            if "if" in step:
                found[(job_name, step_key(step))] = step["if"]
    assert found == GATING_STEP_IFS
    assert workflow["jobs"]["ci"]["if"] == GATING_JOB_IFS["ci"]


def test_the_tests_matrix_covers_every_python_and_resolution(workflow: dict[str, Any]) -> None:
    assert workflow["jobs"]["tests"]["strategy"] == {
        "fail-fast": False,
        "matrix": {
            "python": ["3.11", "3.12", "3.13", "3.14"],
            "resolution": ["lowest-direct", "highest"],
            "include": [{"python": "3.13", "resolution": "locked"}],
        },
    }


@pytest.mark.parametrize(
    ("job", "step", "command"),
    [
        (
            "tests",
            "Import the package with warnings as errors",
            'python -W error -c "import permit_mcp"',
        ),
        (
            "tests",
            "Offline tests",
            'python -m pytest -q -W error --junitxml="$RUNNER_TEMP/junit.xml"',
        ),
        (
            "tests",
            "Check that every test ran",
            (
                "uv run --no-project --python 3.11 python .github/scripts/check_junit.py"
                ' "$RUNNER_TEMP/junit.xml"'
            ),
        ),
    ],
)
def test_the_test_legs_run_their_checks(
    workflow: dict[str, Any], job: str, step: str, command: str
) -> None:
    found = find_step(workflow, (job, step))
    assert " ".join(found["run"].split()) == command
    assert "if" not in found


def test_the_history_scan_fetches_the_whole_history(workflow: dict[str, Any]) -> None:
    checkout = workflow["jobs"]["gitleaks"]["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["fetch-depth"] == 0


def test_no_cache_can_decide_a_gate(workflow: dict[str, Any]) -> None:
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            assert not step.get("uses", "").startswith("actions/cache"), step
            assert step.get("with", {}).get("enable-cache", False) is False, step


def test_scheduled_and_manual_runs_have_their_own_concurrency_group(
    workflow: dict[str, Any],
) -> None:
    for name, job in workflow["jobs"].items():
        group = " ".join(job["concurrency"]["group"].split())
        if name in {"dependency-review", "mutation"}:
            assert job["if"] == PULL_REQUEST_IF
            assert group == f"${{{{ github.workflow }}}}-${{{{ github.ref }}}}-{name}"
        elif name == "e2e":
            assert group == E2E_GROUP
        elif name == "notify":
            assert group.endswith("-${{ github.run_id }}")
        else:
            assert group.endswith(RUN_ID_GROUP), name
        expected_cancel = (
            "false" if name in {"notify", "e2e"} else "${{ github.event_name == 'pull_request' }}"
        )
        assert str(job["concurrency"]["cancel-in-progress"]).lower() == expected_cancel.lower()


def test_the_stdlib_scripts_run_on_the_floor_python(workflow: dict[str, Any]) -> None:
    calls = 0
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            run = " ".join(step.get("run", "").split())
            assert "python3" not in run, step_key(step)
            for match in re.finditer(r"(\S+ \S+ \S+ \S+ \S+ \S+) \.github/scripts/(\w+)\.py", run):
                if match.group(2) == "mutation_gate":
                    # It runs mutmut, which runs the test suite: the project environment.
                    assert match.group(1).endswith(" uv run --locked python"), run
                    continue
                assert match.group(1) == "uv run --no-project --python 3.11 python", run
                calls += 1
    assert calls >= 8


# --- the docs build ---------------------------------------------------------------

BUILD_DOCS = SCRIPTS / "build-docs.sh"
DOCS_STEP = ("docs", "Build the docs site")
CLEAN_BUILD = "Build started\nNo issues found\nBuild finished in 0.35s\n"
# What Zensical 0.0.65 prints for a Raises entry without a colon: Griffe's warning,
# then the lines of a clean build, and exit 0 even with --strict.
GRIFFE_WARNING = (
    "Build started\n"
    "griffe: src/permit_mcp/identity.py:43: Failed to get 'exception: description' pair"
    " from 'ValueError `user_key` is empty or only whitespace.'\n"
    "No issues found\nBuild finished in 0.33s\n"
)
# An unresolved cross-reference, as --strict reports it before it stops with status 1.
UNRESOLVED_REFERENCE = (
    "Build started\n"
    "\x1b[33mWarning:\x1b[0m unresolved autoref `permit_mcp.identity.NoSuchError` in"
    " reference/api.md\n1 issue found\nRuntimeError: Aborted because --strict flag is set\n"
)


PLANTED_MKDOCS = (
    "site_url: https://example.github.io/planted/\n\nnav:\n  - Home: index.md\n\nplugins: []\n"
)


def plant_docs_checkout(tmp_path: Path) -> None:
    """A checkout with check_site.py, a one-page mkdocs.yml and its page."""
    (tmp_path / ".github" / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPTS / "check_site.py", tmp_path / ".github" / "scripts" / "check_site.py")
    (tmp_path / "mkdocs.yml").write_text(PLANTED_MKDOCS, encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "index.md").write_text("# Home\n", encoding="utf-8")


def run_build_docs(tmp_path: Path, uv_script: str) -> subprocess.CompletedProcess[str]:
    """Run build-docs.sh in a planted checkout, with `uv_script` as uv.

    The stand-in records each command line in uv.txt. `uv run --no-project ...
    python` runs the rest with this Python, so the real check_site.py runs; the
    other commands run `uv_script`.
    """
    plant_docs_checkout(tmp_path)
    stand_in(
        tmp_path / "bin",
        "uv",
        f'echo "$*" >>"{tmp_path}/uv.txt"\n'
        "if [[ $2 == --no-project ]]; then\n"
        "  while [[ $1 != python ]]; do shift; done\n"
        "  shift\n"
        f'  exec "{sys.executable}" "$@"\n'
        f"fi\n{uv_script}",
    )
    return subprocess.run(
        [tool("bash"), str(BUILD_DOCS)],
        env={"PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}"},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def build_with(
    tmp_path: Path, log: str, *, status: int = 0, html: str | None = "<a href='x/'>x</a>"
) -> subprocess.CompletedProcess[str]:
    """Run build-docs.sh with a Zensical that prints `log` and exits with `status`.

    The build writes `html` as site/index.html, and no site when `html` is None.
    """
    log_file = tmp_path / "zensical.log"
    log_file.write_text(log, encoding="utf-8")
    write_site = ""
    if html is not None:
        page = tmp_path / "page.html"
        page.write_text(html, encoding="utf-8")
        write_site = f'mkdir -p site && cp "{page}" site/index.html\n'
    return run_build_docs(
        tmp_path,
        'if [[ " $* " != *" zensical "* ]]; then exit 0; fi\n'
        f'{write_site}cat "{log_file}"\nexit {status}',
    )


def test_the_docs_job_builds_with_the_script(workflow: dict[str, Any], needed: list[str]) -> None:
    assert "docs" in needed
    assert find_step(workflow, DOCS_STEP)["run"].strip() == ".github/scripts/build-docs.sh"


def test_build_docs_writes_the_pages_builds_strict_from_a_clean_cache_and_checks_the_site(
    tmp_path: Path,
) -> None:
    completed = build_with(tmp_path, CLEAN_BUILD)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "The docs built with no warning." in completed.stdout
    assert (tmp_path / "uv.txt").read_text(encoding="utf-8").splitlines() == [
        "run --locked --group docs python scripts/docs_pages.py",
        "run --locked --group docs zensical build --strict --clean",
        "run --no-project --python 3.11 python .github/scripts/check_site.py",
    ]


def test_build_docs_fails_on_a_griffe_warning_that_zensical_lets_pass(tmp_path: Path) -> None:
    completed = build_with(tmp_path, GRIFFE_WARNING)
    assert completed.returncode == 1
    error = completed.stdout.split("::error title=Docs::", 1)[1]
    assert "griffe: src/permit_mcp/identity.py:43: Failed to get" in error
    assert "Build started" not in error, "only the unexpected lines are repeated"


@pytest.mark.parametrize(
    "line",
    [
        "WARNING -  mkdocs_autorefs: Could not find cross-reference target",
        "Warning: anchor does not exist",
        "Something a clean build does not print",
    ],
)
def test_build_docs_fails_on_any_line_a_clean_build_does_not_print(
    tmp_path: Path, line: str
) -> None:
    completed = build_with(tmp_path, CLEAN_BUILD + line + "\n")
    assert completed.returncode == 1
    assert line in completed.stdout.split("::error title=Docs::", 1)[1]


def test_build_docs_strips_zensical_s_colours(tmp_path: Path) -> None:
    coloured = "\x1b[1mBuild started\x1b[0m\nNo issues found\nBuild finished in 12ms\n"
    completed = build_with(tmp_path, coloured)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "\x1b" not in completed.stdout


def test_build_docs_fails_when_strict_stops_the_build(tmp_path: Path) -> None:
    completed = build_with(tmp_path, UNRESOLVED_REFERENCE, status=1)
    assert completed.returncode == 1
    assert "Warning: unresolved autoref" in completed.stdout
    assert "::error title=Docs::zensical build exited 1" in completed.stdout


def test_build_docs_fails_when_a_nav_page_was_not_built(tmp_path: Path) -> None:
    completed = build_with(tmp_path, CLEAN_BUILD, html=None)
    assert completed.returncode == 1
    assert "nav lists index.md, but the build wrote no site/index.html" in completed.stdout


def test_build_docs_fails_on_a_root_relative_link(tmp_path: Path) -> None:
    completed = build_with(tmp_path, CLEAN_BUILD, html="<a href='/reference/'>r</a>")
    assert completed.returncode == 1
    assert "/reference/ starts with / outside the site's path /planted/" in completed.stdout


def test_build_docs_names_a_missing_snippet_when_the_build_also_failed(tmp_path: Path) -> None:
    snippet = tmp_path / "snippet.md"
    snippet.write_text('--8<-- "GONE.md"\n', encoding="utf-8")
    completed = run_build_docs(
        tmp_path,
        f'if [[ " $* " == *" zensical "* ]]; then cp "{snippet}" docs/index.md; exit 1; fi',
    )
    assert completed.returncode == 1
    assert "docs/index.md includes GONE.md, which is missing" in completed.stdout


def test_build_docs_exits_2_when_the_build_did_not_finish(tmp_path: Path) -> None:
    assert build_with(tmp_path, "Build started\n").returncode == 2


def test_build_docs_stops_when_the_pages_cannot_be_written(tmp_path: Path) -> None:
    completed = run_build_docs(tmp_path, "exit 3")
    assert completed.returncode == 3
    assert len((tmp_path / "uv.txt").read_text(encoding="utf-8").splitlines()) == 1


# --- the end-to-end suite ---------------------------------------------------------

E2E_STEP = ("e2e", "End-to-end tests")
E2E_JUNIT_STEP = ("e2e", "Check that every test ran")
E2E_GROUP = "${{ github.repository }}-permit-e2e-project"


def secret_reads(node: object, where: str = "") -> list[str]:
    """Return the path in the workflow of every string that reads the secrets context."""
    if isinstance(node, dict):
        return [
            found for key, value in node.items() for found in secret_reads(value, f"{where}.{key}")
        ]
    if isinstance(node, list):
        return [
            found
            for index, value in enumerate(node)
            for found in secret_reads(value, f"{where}[{index}]")
        ]
    return [where] if isinstance(node, str) and "secrets" in node else []


def step_index(workflow: dict[str, Any], job: str, key: str) -> int:
    keys = [step_key(step) for step in workflow["jobs"][job]["steps"]]
    return keys.index(key)


def test_only_the_e2e_tests_and_notify_read_secrets(workflow: dict[str, Any]) -> None:
    e2e = step_index(workflow, "e2e", E2E_STEP[1])
    slack = step_index(workflow, "notify", "slackapi/slack-github-action")
    assert workflow["jobs"]["e2e"]["environment"] == "e2e"
    assert secret_reads(workflow["jobs"]) == [
        f".e2e.steps[{e2e}].env.PERMIT_E2E_PROJECT_API_KEY",
        f".e2e.steps[{e2e}].env.PERMIT_E2E_PROJECT_ID",
        ".notify.env.SLACK_WEBHOOK_URL",
        f".notify.steps[{slack}].with.webhook",
    ]
    assert find_step(workflow, E2E_STEP)["env"] == {
        "PERMIT_E2E_PROJECT_API_KEY": "${{ secrets.PERMIT_E2E_PROJECT_API_KEY }}",
        "PERMIT_E2E_PROJECT_ID": "${{ secrets.PERMIT_E2E_PROJECT_ID }}",
    }


def test_the_e2e_job_runs_the_selected_suite_and_checks_that_every_test_ran(
    workflow: dict[str, Any],
) -> None:
    job = workflow["jobs"]["e2e"]
    assert job["if"] == SCHEDULED_IF
    assert job["permissions"] == {"contents": "read"}
    assert [step_key(step) for step in job["steps"]] == [
        "actions/checkout",
        "astral-sh/setup-uv",
        "Install from uv.lock",
        E2E_STEP[1],
        E2E_JUNIT_STEP[1],
    ]
    assert all("if" not in step for step in job["steps"])
    assert find_step(workflow, ("e2e", "Install from uv.lock"))["run"] == "uv sync --locked"
    assert " ".join(find_step(workflow, E2E_STEP)["run"].split()) == (
        "python -m pytest -q -W error -m e2e -p no:cacheprovider"
        ' --junitxml="$RUNNER_TEMP/junit.xml" tests/e2e'
    )
    assert " ".join(find_step(workflow, E2E_JUNIT_STEP)["run"].split()) == (
        "uv run --no-project --python 3.11 python .github/scripts/check_junit.py"
        ' "$RUNNER_TEMP/junit.xml"'
    )


def test_e2e_runs_against_the_project_one_at_a_time_and_is_never_cancelled(
    workflow: dict[str, Any],
) -> None:
    assert workflow["jobs"]["e2e"]["concurrency"] == {
        "group": E2E_GROUP,
        "cancel-in-progress": False,
    }
