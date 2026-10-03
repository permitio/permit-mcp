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
        assert job["timeout-minutes"] <= 15, name


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


def run_ci(
    workflow: dict[str, Any], tmp_path: Path, needs: str, event: str, advisory: str = ""
) -> subprocess.CompletedProcess[str]:
    env = {"NEEDS": needs, "EVENT": event, "ADVISORY_JOBS": advisory}
    return run_step(workflow, CI_STEP, tmp_path, env)


def test_ci_is_named_ci_and_runs_whatever_happened_to_its_needs(workflow: dict[str, Any]) -> None:
    ci = workflow["jobs"]["ci"]
    assert ci["name"] == "CI"
    assert ci["if"] == "always()"


def test_no_job_is_advisory_yet(workflow: dict[str, Any]) -> None:
    assert find_step(workflow, CI_STEP)["env"]["ADVISORY_JOBS"] == ""


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


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
def test_ci_accepts_a_skipped_dependency_review_off_pull_requests(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path, event: str
) -> None:
    needs = results(needed, {"dependency-review": "skipped"})
    completed = run_ci(workflow, tmp_path, needs, event)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_ci_fails_a_skipped_dependency_review_on_a_pull_request(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path
) -> None:
    needs = results(needed, {"dependency-review": "skipped"})
    completed = run_ci(workflow, tmp_path, needs, "pull_request")
    assert completed.returncode == 1
    assert "Jobs that did not succeed: dependency-review skipped" in completed.stdout


@pytest.mark.parametrize("result", ["failure", "cancelled"])
def test_ci_fails_a_dependency_review_that_ran_and_failed_on_a_push(
    workflow: dict[str, Any], needed: list[str], tmp_path: Path, result: str
) -> None:
    needs = results(needed, {"dependency-review": result})
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
    assert set(notify["needs"]) == {"audit", "ci"}
    assert notify["if"] == (
        "always() && github.workflow == 'CI' &&"
        " (github.event_name == 'schedule' || github.event_name == 'workflow_dispatch')"
    )


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
    assert f"CI needs every job: {', '.join(sorted(needed))}. Advisory: none." in (completed.stdout)


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


def gate_with_a_vulnerable_dev_tree(
    workflow: dict[str, Any], tmp_path: Path, gate_dev_tree: str
) -> subprocess.CompletedProcess[str]:
    package = [{"Name": "p", "Version": "1"}]
    clean = {"Results": [{"Target": "requirements.txt", "Packages": package}]}
    advisory = {"VulnerabilityID": "CVE-1", "PkgName": "p", "Severity": "HIGH", "FixedVersion": "2"}
    vulnerable = {"Results": [{**clean["Results"][0], "Vulnerabilities": [advisory]}]}
    for tree in audit_trees(workflow):
        report = vulnerable if tree == "dev-ceiling" else clean
        plant_tree(tmp_path / "audit", tree, report, pinned=1)
    env = {"AUDIT_DIR": str(tmp_path / "audit"), "GATE_DEV_TREE": gate_dev_tree}
    return run_step(workflow, AUDIT_GATE_STEP, tmp_path, env)


@pytest.mark.parametrize("gate_dev_tree", ["true", "null"], ids=["true", "not called"])
def test_the_audit_gates_on_the_dev_tree_by_default(
    workflow: dict[str, Any], tmp_path: Path, gate_dev_tree: str
) -> None:
    completed = gate_with_a_vulnerable_dev_tree(workflow, tmp_path, gate_dev_tree)
    assert completed.returncode == 1
    assert "::error title=Dependency audit::Fixable HIGH or CRITICAL" in completed.stdout


def test_a_caller_can_leave_the_dev_tree_ungated(workflow: dict[str, Any], tmp_path: Path) -> None:
    completed = gate_with_a_vulnerable_dev_tree(workflow, tmp_path, "false")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "dev-ceiling is reported in the summary but not gated" in completed.stdout


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
    assert audit_trees(workflow) == compiled == ["runtime-ceiling", "runtime-floor", "dev-ceiling"]


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
    --group) tree={tmp_path}/dev.txt; shift ;;
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
    assert len(compiles) == 3
    ceiling, floor, dev = compiles
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
        '[dependency-groups]\ndev = ["pytest==9.1.1"]\n'
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


def run_scan(
    workflow: dict[str, Any],
    tmp_path: Path,
    *,
    output: str,
    exit_code: int,
    git_answers: tuple[str, str] = ("false", "14"),
) -> subprocess.CompletedProcess[str]:
    """Run the step with stand-ins for gitleaks, and for git answering (is shallow, commits)."""
    shallow, commits = git_answers
    stand_in(
        tmp_path / "bin",
        "git",
        f'for arg in "$@"; do case $arg in rev-parse) echo {shallow} ;;'
        f" rev-list) echo {commits} ;; esac; done",
    )
    stand_in(tmp_path / "bin", "gitleaks", f"cat >&2 <<'OUT'\n{output}\nOUT\nexit {exit_code}")
    return run_step(workflow, GITLEAKS_STEP, tmp_path, {})


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
    assert run_step(workflow, GITLEAKS_STEP, tmp_path, {}).returncode == 0
    args = (tmp_path / "args").read_text().splitlines()
    assert args[0] == "git"
    assert args[args.index("--config") + 1] == f"{tmp_path}/gitleaks.toml"
    assert (tmp_path / "gitleaks.toml").read_text() == "[extend]\nuseDefault = true\n"
    ignore_path = Path(args[args.index("--gitleaks-ignore-path") + 1])
    assert ignore_path.is_dir()
    assert not any(ignore_path.iterdir())
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
    workflow: dict[str, Any], tmp_path: Path, repo: Path
) -> subprocess.CompletedProcess[str]:
    """Run the step with the pinned gitleaks in the planted repository."""
    gitleaks = tool("gitleaks")
    version = subprocess.run([gitleaks, "version"], capture_output=True, text=True, check=True)
    assert version.stdout.strip() == pinned_version("GITLEAKS_URL", workflow)
    (tmp_path / "bin").mkdir(exist_ok=True)
    shutil.copy(gitleaks, tmp_path / "bin" / "gitleaks")
    env = {**GIT_ENV, "HOME": os.environ["HOME"]}
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
    assert "| Control plane | 24 | 13 | 0 | 11 |" in text
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
}
GATING_JOB_IFS = {"ci": "always()", "dependency-review": "github.event_name == 'pull_request'"}
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
        if name == "dependency-review":
            assert job["if"] == "github.event_name == 'pull_request'"
        elif name == "notify":
            assert group.endswith("-${{ github.run_id }}")
        else:
            assert group.endswith(RUN_ID_GROUP), name
        expected_cancel = (
            "false" if name == "notify" else "${{ github.event_name == 'pull_request' }}"
        )
        assert str(job["concurrency"]["cancel-in-progress"]).lower() == expected_cancel.lower()


def test_the_stdlib_scripts_run_on_the_floor_python(workflow: dict[str, Any]) -> None:
    calls = 0
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            run = " ".join(step.get("run", "").split())
            assert "python3" not in run, step_key(step)
            for match in re.finditer(r"(\S+ \S+ \S+ \S+ \S+ \S+) \.github/scripts/\w+\.py", run):
                assert match.group(1) == "uv run --no-project --python 3.11 python", run
                calls += 1
    assert calls >= 7
