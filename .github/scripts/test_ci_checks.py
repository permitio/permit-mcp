"""Tests for the bash scripts ci.yml runs.

ci-steps.sh, audit-report.sh, audit-deps.sh, gitleaks-scan.sh and build-docs.sh each
run against planted job results, workflows, reports, checkouts or repositories, with
stand-ins for the tools they call, and the tests check their exit status and output; a
few run the pinned gitleaks and Trivy. The tests need bash, git, jq, yq (mikefarah v4)
and the pinned gitleaks and Trivy on PATH; the audit-scripts job installs the last two,
and GitHub's ubuntu-24.04 runners have the rest.

Run with:
uv run --only-dev pytest -c .github/scripts/pytest.ini .github/scripts/test_ci_checks.py
"""

from __future__ import annotations

import copy
import json
import re
import secrets
import shutil
import string
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from check_floors import floors
from harness import REPO_ROOT, SCRIPTS, git, read_workflow, run_script, stand_in, tool
from test_api_coverage import (
    AR,
    AR_CREATE,
    CHECK,
    E2E_API,
    E2E_CONTAINER_PDP,
    E2E_ORIGINS,
    E2E_TEST,
    ORIGINS,
    SCOPE,
    as_openapi,
    line,
    operation_row,
    wire_record,
)
from test_workflows import NEEDED

if TYPE_CHECKING:
    from collections.abc import Callable

CI_ENV = {
    "EXPECTED_JOBS": str(len(NEEDED)),
    "ADVISORY_JOBS": "e2e",
    "PULL_REQUEST_JOBS": "dependency-review mutation",
    "SCHEDULED_JOBS": "e2e",
}
TREES = ["runtime-ceiling", "runtime-floor", "dev-ceiling", "docs-ceiling", "example-ceiling"]


def python_uv(tmp_path: Path, otherwise: str = "") -> None:
    """Plant a uv that runs this interpreter for `uv run --no-project ... python ARGS`.

    Any other call is logged to RUNNER_TEMP/uv.log, then runs `otherwise`.
    """
    stand_in(
        tmp_path,
        "uv",
        "if [[ $1 == run && $2 == --no-project ]]; then\n"
        "  while [[ $1 != python ]]; do shift; done\n"
        f'  shift\n  exec "{sys.executable}" "$@"\nfi\n'
        f'echo "$*" >>"$RUNNER_TEMP/uv.log"\n{otherwise}',
    )


def workflow_commands(completed: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in completed.stdout.splitlines() if line.startswith("::")]


def floor_and_ceiling() -> dict[str, list[str]]:
    """The project's direct dependencies pinned at their floors, and above them."""
    declared = floors(REPO_ROOT / "pyproject.toml")
    return {
        "floor": [f"{name}=={version}" for name, version in declared.items()],
        "ceiling": [f"{name}==999.0" for name in declared],
    }


def test_every_script_stops_on_an_error_an_unset_variable_and_a_failed_pipe() -> None:
    for script in SCRIPTS.glob("*.sh"):
        assert "\nset -euo pipefail\n" in script.read_text(), script.name


@pytest.mark.parametrize("script", ["ci-steps.sh", "audit-report.sh", "release-checks.sh"])
def test_an_unknown_subcommand_exits_2(tmp_path: Path, script: str) -> None:
    completed = run_script(tmp_path, script, "nothing")
    assert completed.returncode == 2
    assert "usage:" in completed.stderr


@pytest.mark.parametrize(
    ("argv", "env", "unset"),
    [
        (["ci-steps.sh", "results"], {**CI_ENV, "NEEDS": "{}", "EVENT": "push"}, "WORKFLOW"),
        (["audit-report.sh", "gate"], {"AUDIT_TREES": "runtime-floor"}, "AUDIT_DIR"),
        (["gitleaks-scan.sh", "gitleaks"], {}, "EVENT"),
        (["release-checks.sh", "tag"], {"EVENT": "release", "TAG": "v1.2.3"}, "PRERELEASE"),
    ],
)
def test_a_script_missing_an_input_did_not_run(
    tmp_path: Path, argv: list[str], env: dict[str, str], unset: str
) -> None:
    completed = run_script(tmp_path, *argv, env=env)
    assert completed.returncode == 2
    assert workflow_commands(completed) == [f"::error title={argv[0]}::did not run: {unset} unset"]


# --- ci-steps.sh results: the CI job ------------------------------------------------

FAILED = "::error title=CI::Jobs that did not succeed: "
ADVISORY = "::warning title=CI::Advisory job did not succeed: "
DR = "dependency-review"


def results(overrides: dict[str, str], jobs: list[str] = NEEDED) -> str:
    """`toJSON(needs)` for the given jobs, each a success unless `overrides` says otherwise."""
    return json.dumps(
        {job: {"result": overrides.get(job, "success"), "outputs": {}} for job in jobs}
    )


def own_run(event: str) -> str:
    return (
        f"::error title=CI::e2e was skipped on CI's own {event} run, which it must run in:"
        " its if: no longer matches this run."
    )


def scheduled_only(event: str) -> str:
    return (
        "::notice title=CI::e2e runs in CI's scheduled and manual runs only; skipped on this"
        f" {event} run."
    )


def pr_only(job: str, event: str) -> str:
    return f"::notice title=CI::{job} runs on pull requests only; skipped on this {event} run."


@pytest.mark.parametrize(
    ("overrides", "run", "status", "says"),
    [
        ({}, "CI pull_request", 0, []),
        ({}, "CI schedule", 0, []),
        ({"tests": "failure"}, "CI push", 1, [FAILED + "tests failure"]),
        ({"tests": "cancelled"}, "CI pull_request", 1, [FAILED + "tests cancelled"]),
        ({"tests": "skipped"}, "CI pull_request", 1, [FAILED + "tests skipped"]),
        ({"audit": "failure", "docs": "cancelled"}, "CI push", 1,
         [FAILED + "audit failure, docs cancelled"]),
        ({"mutation": "skipped"}, "CI push", 0, [pr_only("mutation", "push")]),
        ({DR: "skipped", "mutation": "skipped"}, "CI schedule", 0,
         [pr_only(DR, "schedule"), pr_only("mutation", "schedule")]),
        ({DR: "skipped"}, "Release workflow_dispatch", 0, [pr_only(DR, "workflow_dispatch")]),
        ({"mutation": "skipped"}, "CI pull_request", 1, [FAILED + "mutation skipped"]),
        ({DR: "cancelled"}, "CI push", 1, [FAILED + "dependency-review cancelled"]),
        ({DR: "skipped", "audit": "skipped"}, "CI push", 1,
         [pr_only(DR, "push"), FAILED + "audit skipped"]),
        ({"e2e": "failure"}, "CI schedule", 0, [ADVISORY + "e2e failure"]),
        ({"e2e": "cancelled"}, "CI push", 0, [ADVISORY + "e2e cancelled"]),
        ({"e2e": "skipped"}, "CI schedule", 1, [own_run("schedule"), FAILED + "e2e skipped"]),
        ({"e2e": "skipped"}, "CI workflow_dispatch", 1,
         [own_run("workflow_dispatch"), FAILED + "e2e skipped"]),
        ({"e2e": "skipped", "audit": "failure"}, "CI schedule", 1,
         [own_run("schedule"), FAILED + "audit failure, e2e skipped"]),
        ({"e2e": "skipped"}, "CI pull_request", 0, [scheduled_only("pull_request")]),
        ({"e2e": "skipped"}, "CI push", 0, [scheduled_only("push")]),
        ({"e2e": "skipped"}, "Release release", 0, [scheduled_only("release")]),
        ({"e2e": "skipped"}, "Release workflow_dispatch", 0, [scheduled_only("workflow_dispatch")]),
    ],
)  # fmt: skip
def test_ci_fails_unless_each_needed_job_succeeded_or_may_be_skipped_or_fail(
    tmp_path: Path, overrides: dict[str, str], run: str, status: int, says: list[str]
) -> None:
    workflow, event = run.split()
    env = {**CI_ENV, "NEEDS": results(overrides), "EVENT": event, "WORKFLOW": workflow}
    completed = run_script(tmp_path, "ci-steps.sh", "results", env=env)
    assert completed.returncode == status, completed.stdout + completed.stderr
    assert workflow_commands(completed) == says
    assert completed.stdout.endswith(f"All {len(NEEDED)} jobs passed.\n") == (status == 0)


@pytest.mark.parametrize(
    ("overrides", "status", "says"),
    [
        ({"tests": "failure"}, 0, [ADVISORY + "tests failure"]),
        ({"tests": "skipped", "package": "failure"}, 1,
         [ADVISORY + "tests skipped", FAILED + "package failure"]),
        ({}, 0, []),
    ],
)  # fmt: skip
def test_an_advisory_job_may_fail_with_a_warning(
    tmp_path: Path, overrides: dict[str, str], status: int, says: list[str]
) -> None:
    env = {**CI_ENV, "ADVISORY_JOBS": "tests", "NEEDS": results(overrides)}
    env |= {"EVENT": "pull_request", "WORKFLOW": "CI"}
    completed = run_script(tmp_path, "ci-steps.sh", "results", env=env)
    assert completed.returncode == status
    assert workflow_commands(completed) == says


@pytest.mark.parametrize(
    ("needs", "says"),
    [
        (results({}, NEEDED[1:]), f"{len(NEEDED) - 1} job results, expected {len(NEEDED)}"),
        (results({}, [*NEEDED, "extra"]), f"{len(NEEDED) + 1} job results, expected"),
        ("not json", "Could not read the job results"),
        ("[1]", "Could not read the job results"),
        ("", f"0 job results, expected {len(NEEDED)}"),
        ("{}", f"0 job results, expected {len(NEEDED)}"),
    ],
    ids=["one missing", "one extra", "not json", "not an object", "empty", "no job"],
)
def test_ci_exits_2_unless_the_expected_results_arrive(
    tmp_path: Path, needs: str, says: str
) -> None:
    env = {**CI_ENV, "NEEDS": needs, "EVENT": "pull_request", "WORKFLOW": "CI"}
    completed = run_script(tmp_path, "ci-steps.sh", "results", env=env)
    assert completed.returncode == 2
    assert says in completed.stdout


# --- ci-steps.sh needs: the prek job's check of CI's needs ----------------------------


def ci_env(workflow: dict[str, Any], **values: object) -> None:
    (step,) = [
        s for s in workflow["jobs"]["ci"]["steps"] if s.get("name") == "Check the needed jobs"
    ]
    step["env"].update(values)


def set_needs(workflow: dict[str, Any], needs: list[str]) -> None:
    workflow["jobs"]["ci"]["needs"] = needs
    ci_env(workflow, EXPECTED_JOBS=len(needs))


def rename_ci_step(workflow: dict[str, Any]) -> None:
    for step in workflow["jobs"]["ci"]["steps"]:
        step["name"] = "Renamed"


@pytest.mark.parametrize(
    ("edit", "status", "says"),
    [
        (lambda _: None, 0, f"CI needs every job: {', '.join(NEEDED)}. Advisory: e2e."),
        (lambda w: set_needs(w, [j for j in NEEDED if j != "package"]), 1, "< package"),
        (lambda w: w["jobs"].update({"new-job": {"steps": []}}), 1, "< new-job"),
        (lambda w: set_needs(w, [*NEEDED, "no-such-job"]), 1, "> no-such-job"),
        (lambda w: set_needs(w, [*NEEDED, "tests"]), 1, "CI needs tests more than once"),
        (lambda w: w["jobs"]["notify"].update({"needs": ["ci"]}), 0, "Advisory: e2e."),
        (lambda w: ci_env(w, ADVISORY_JOBS="tests"), 0, "Advisory: tests."),
        (lambda w: ci_env(w, ADVISORY_JOBS="tests gone"), 1, "lists gone, which CI does not need"),
        (lambda w: ci_env(w, ADVISORY_JOBS="tests tests"), 1, "ADVISORY_JOBS lists tests twice"),
        (lambda w: ci_env(w, EXPECTED_JOBS=11), 1, "CI job is 11, but CI needs 12 jobs"),
        (lambda w: ci_env(w, EXPECTED_JOBS=13), 1, "CI job is 13, but CI needs 12 jobs"),
        (rename_ci_step, 1, "EXPECTED_JOBS in the CI job is not set, but CI needs 12"),
        (lambda w: w.clear(), 2, "No jobs read from"),
        (lambda w: w.update(jobs={"ci": {"steps": []}, "notify": {"steps": []}}), 2, "No jobs"),
    ],
    ids=[
        "committed", "a job not needed", "a new job", "a job that does not exist", "a job twice",
        "notify", "an advisory job", "an advisory job not needed", "an advisory job twice",
        "one too few", "one too many", "the CI step renamed", "empty", "only ci and notify",
    ],
)  # fmt: skip
def test_the_needs_check(
    tmp_path: Path, edit: Callable[[dict[str, Any]], object], status: int, says: str
) -> None:
    planted = copy.deepcopy(read_workflow("ci.yml"))
    edit(planted)
    path = tmp_path / "planted.yml"
    path.write_text(json.dumps(planted), encoding="utf-8")
    completed = run_script(tmp_path, "ci-steps.sh", "needs", path)
    assert completed.returncode == status, completed.stdout + completed.stderr
    assert says in completed.stdout


def test_the_needs_check_reads_ci_yml_and_exits_2_without_it(tmp_path: Path) -> None:
    assert run_script(tmp_path, "ci-steps.sh", "needs").returncode == 0
    assert run_script(tmp_path, "ci-steps.sh", "needs", tmp_path / "gone.yml").returncode == 2


# --- ci-steps.sh hooks ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("passed", "extra", "prek_status", "status"),
    [
        (21, "check json....(no files to check)Skipped", 0, 0),
        (20, "ruff check....(no files to check)Skipped", 0, 2),
        (22, "", 0, 2),
        (21, "mypy....Failed", 1, 1),
    ],
    ids=["every hook passed", "a hook skipped", "a hook added", "a hook failed"],
)
def test_the_hook_count(
    tmp_path: Path, passed: int, extra: str, prek_status: int, status: int
) -> None:
    lines = "\n".join([*(f"hook {index}....Passed" for index in range(passed)), extra])
    stand_in(tmp_path, "uv", f"cat <<'OUT'\n{lines}\nOUT\nexit {prek_status}")
    completed = run_script(tmp_path, "ci-steps.sh", "hooks", env={"EXPECTED_HOOKS": "21"})
    assert completed.returncode == status, completed.stdout + completed.stderr
    if status == 2:
        assert f"{passed} hooks passed, expected 21" in completed.stdout


# --- ci-steps.sh install: the tests job's legs on the ranges -------------------------------


def install(
    tmp_path: Path, resolution: str, *, floor: str = "floor", freeze: str = "resolved"
) -> subprocess.CompletedProcess[str]:
    """Run the install with a planted uv.

    Its compile resolves the `floor` tree (the project's floors) at lowest-direct and the
    ceiling otherwise, and its freeze prints the `freeze` tree, by default the resolved one.
    """
    planted = floor_and_ceiling() | {"older": ["mcp==0.1"], "empty": []}
    planted["more"] = [*planted["floor"], "pytest==9.1.1"]
    for tree, pins in planted.items():
        (tmp_path / f"{tree}.txt").write_text("".join(f"{pin}\n    # via x\n" for pin in pins))
    python_uv(
        tmp_path,
        f"""cd "$RUNNER_TEMP"
case "$1 $2" in
  "pip compile")
    tree=ceiling
    [[ " $* " == *" --resolution lowest-direct "* ]] && tree={floor}
    cp "$tree.txt" resolved.txt
    while [[ $1 != -o ]]; do shift; done
    cp "$tree.txt" "$2" ;;
  "pip freeze") cat {freeze}.txt ;;
esac""",
    )
    return run_script(tmp_path, "ci-steps.sh", "install", env={"RESOLUTION": resolution})


@pytest.mark.parametrize("resolution", ["lowest-direct", "highest"])
def test_a_leg_installs_and_checks_its_resolution(tmp_path: Path, resolution: str) -> None:
    completed = install(tmp_path, resolution)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    count = len(floor_and_ceiling()["floor"])
    assert f"{count} runtime packages at the {resolution} resolution." in completed.stdout
    at_floor = "Every direct dependency is at its floor" in completed.stdout
    assert at_floor == (resolution == "lowest-direct")
    compile_call, install_call, _ = (tmp_path / "uv.log").read_text().splitlines()
    assert compile_call.startswith("pip compile --quiet --no-sources --exclude-newer false")
    assert f"--resolution {resolution} pyproject.toml -o {tmp_path}/runtime.txt" in compile_call
    assert install_call == (
        f"pip install --no-sources --exclude-newer false . --group dev -c {tmp_path}/runtime.txt"
    )


@pytest.mark.parametrize(
    ("floor", "freeze", "status", "says"),
    [
        ("ceiling", "resolved", 1, "its floor is"),
        ("floor", "older", 1, "::error title=Install::Not installed as resolved: "),
        ("floor", "more", 0, "runtime packages at the lowest-direct resolution."),
        ("empty", "resolved", 2, "::error title=Install::The lowest-direct resolution is empty."),
    ],
    ids=["above the floors", "other versions installed", "the dev group too", "nothing resolved"],
)
def test_a_leg_fails_unless_it_installed_its_resolution(
    tmp_path: Path, floor: str, freeze: str, status: int, says: str
) -> None:
    completed = install(tmp_path, "lowest-direct", floor=floor, freeze=freeze)
    assert completed.returncode == status, completed.stdout + completed.stderr
    assert says in completed.stdout


# --- ci-steps.sh coverage: the API coverage report ------------------------------------------

COMMITTED_INVENTORY = REPO_ROOT / ".github" / "api-specs" / "control-plane.json"
SERVE_LIVE_SPEC = (
    'echo "$*" >"$RUNNER_TEMP/curl.txt"\n'
    'while [[ $1 != --output ]]; do shift; done\ncp "$RUNNER_TEMP/live.json" "$2"'
)


def coverage(
    tmp_path: Path, run: str, *, drift: bool = False, curl: str = SERVE_LIVE_SPEC
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run the report on `run` ("WORKFLOW EVENT") over a planted record of the wire cases.

    The planted curl serves the committed inventory as the live spec, with
    list_access_requests's resource_instance_id query parameter renamed when `drift`.
    """
    (tmp_path / "api-record.jsonl").write_text("".join(json.dumps(x) + "\n" for x in wire_record()))
    (tmp_path / "api-record.origins.json").write_text(json.dumps(ORIGINS))
    document = as_openapi(COMMITTED_INVENTORY, filler=200)
    if drift:
        path = "/v2/facts/{proj_id}/{env_id}/access_requests/{elements_config_id}/user/{user_id}"
        for parameter in document["paths"][f"{path}/tenant/{{tenant_id}}"]["get"]["parameters"]:
            if parameter["name"] == "resource_instance_id":
                parameter["name"] = "resource_instance"
    (tmp_path / "live.json").write_text(json.dumps(document))
    stand_in(tmp_path, "curl", curl)
    python_uv(tmp_path)
    workflow, event = run.split()
    summary = tmp_path / "summary.md"
    env = {"EVENT": event, "WORKFLOW": workflow, "GITHUB_STEP_SUMMARY": str(summary)}
    completed = run_script(tmp_path, "ci-steps.sh", "coverage", env=env)
    return completed, summary.read_text() if summary.exists() else ""


@pytest.mark.parametrize(
    "run", ["CI pull_request", "CI push", "Release release", "Release workflow_dispatch"]
)
def test_coverage_reads_the_committed_inventory_outside_ci_s_own_runs(
    tmp_path: Path, run: str
) -> None:
    completed, summary = coverage(tmp_path, run, curl="exit 99")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "| Control plane | 23 | 12 | 0 | 11 |" in summary
    assert "- Control plane: `.github/api-specs/control-plane.json`" in summary
    assert "baseline" not in summary


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
@pytest.mark.parametrize("drift", [False, True], ids=["unchanged", "drifted"])
def test_ci_s_own_runs_check_the_live_spec_for_drift(
    tmp_path: Path, event: str, *, drift: bool
) -> None:
    completed, summary = coverage(tmp_path, f"CI {event}", drift=drift)
    curl = (tmp_path / "curl.txt").read_text()
    assert "--fail" in curl
    assert "https://api.permit.io/v2/openapi.json" in curl
    live = tmp_path / "api-specs" / "control-plane.json"
    assert f"- Control plane: `{live}`" in summary
    assert "- Control plane baseline: `.github/api-specs/control-plane.json`" in summary
    assert completed.returncode == int(drift), completed.stdout + completed.stderr
    if drift:
        assert "'query resource_instance_id'" in summary
    else:
        assert live.read_text() == COMMITTED_INVENTORY.read_text()


def test_a_failed_spec_download_exits_2(tmp_path: Path) -> None:
    completed, _ = coverage(tmp_path, "CI schedule", curl="exit 22")
    assert completed.returncode == 2
    assert "::error title=API coverage::Could not download" in completed.stdout
    assert not (tmp_path / "api-specs").exists()


# --- ci-steps.sh e2e-coverage: the e2e job's report, with its end-to-end column ------------


def e2e_coverage(
    tmp_path: Path, e2e: list[dict[str, Any]] | None
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run the e2e job's report on the wire cases' record and `e2e`, the e2e record.

    The planted curl fails: the e2e job's report reads only the committed inventories.
    """
    (tmp_path / "api-record.jsonl").write_text("".join(json.dumps(x) + "\n" for x in wire_record()))
    (tmp_path / "api-record.origins.json").write_text(json.dumps(ORIGINS))
    if e2e is not None:
        (tmp_path / "e2e-record.jsonl").write_text("".join(json.dumps(x) + "\n" for x in e2e))
        (tmp_path / "e2e-record.origins.json").write_text(json.dumps(E2E_ORIGINS))
    stand_in(tmp_path, "curl", "exit 99")
    python_uv(tmp_path)
    summary = tmp_path / "summary.md"
    env = {"GITHUB_STEP_SUMMARY": str(summary)}
    completed = run_script(tmp_path, "ci-steps.sh", "e2e-coverage", env=env)
    return completed, summary.read_text() if summary.exists() else ""


def test_the_e2e_job_s_report_merges_the_end_to_end_record(tmp_path: Path) -> None:
    e2e = [
        line(*SCOPE, E2E_TEST, origin=E2E_API),
        line(*CHECK, E2E_TEST, origin=E2E_CONTAINER_PDP),
        line("POST", AR, E2E_TEST, status=403, origin=E2E_API),
    ]
    completed, summary = e2e_coverage(tmp_path, e2e)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "- Control plane: `.github/api-specs/control-plane.json`" in summary
    assert "- PDP: `.github/api-specs/pdp.json`" in summary
    assert "baseline" not in summary
    assert "- End-to-end record: 3 requests from 1 test; 2 requests got a 2xx" in summary
    assert operation_row(summary, "Control plane", SCOPE)[-1] == "yes"
    assert operation_row(summary, "PDP", CHECK)[-1] == "yes"
    assert operation_row(summary, "Control plane", AR_CREATE)[-1] == "no"
    assert "| Control plane | EAP | 21 | 10 | 0 | 11 | 0 |" in summary


def test_the_e2e_job_s_report_without_an_end_to_end_record_did_not_run(tmp_path: Path) -> None:
    completed, summary = e2e_coverage(tmp_path, None)
    assert completed.returncode == 2
    assert "could not read the end-to-end record" in completed.stdout
    assert ":warning: **The report did not run**" in summary


# --- ci-steps.sh surface: the tool surface report -----------------------------------------


def snapshot(edit: Callable[[dict[str, Any]], object] = lambda _: None) -> dict[str, Any]:
    """The committed surface snapshot, after `edit`."""
    planted: dict[str, Any] = json.loads((REPO_ROOT / "tests/snapshots/surface.json").read_text())
    edit(planted)
    return planted


def commit_snapshot(checkout: Path, planted: object | None, message: str) -> None:
    path = checkout / "tests" / "snapshots" / "surface.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if planted is not None:
        path.write_text(json.dumps(planted))
    git(checkout, "add", ".")
    git(checkout, "commit", "--quiet", "--allow-empty", "-m", message)


def surface(
    tmp_path: Path, base: object | None, later_base: object = None
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run the report in a checkout of a pull request's merge commit, as actions/checkout makes it.

    main holds `base` (None: no snapshot), the pull request the committed one, and HEAD
    merges them, so HEAD^1 is main as merged. With `later_base`, main then moves on to
    it, past the merge.
    """
    checkout = tmp_path / "checkout"
    (checkout / ".github" / "scripts").mkdir(parents=True)
    git(checkout, "init", "--quiet", "--initial-branch=main")
    shutil.copy(SCRIPTS / "surface_diff.py", checkout / ".github" / "scripts")
    commit_snapshot(checkout, base, "base")
    git(checkout, "switch", "--quiet", "-c", "pull-request")
    commit_snapshot(checkout, snapshot(), "change")
    git(checkout, "switch", "--quiet", "main")
    if later_base is not None:
        git(checkout, "switch", "--quiet", "-c", "later")
        commit_snapshot(checkout, later_base, "main moves on")
        git(checkout, "switch", "--quiet", "main")
    git(checkout, "merge", "--quiet", "--no-ff", "-m", "merge", "pull-request")
    python_uv(tmp_path)
    summary = tmp_path / "summary.md"
    env = {"BASE_REF": "main", "GITHUB_STEP_SUMMARY": str(summary)}
    completed = run_script(tmp_path, "ci-steps.sh", "surface", env=env, cwd=checkout)
    return completed, summary.read_text() if summary.exists() else ""


BREAKING = "::warning title=Breaking surface changes::See the job summary."
CANNOT_COMPARE = "::error title=Surface diff::Could not compare the snapshots."
REWORDED = snapshot(lambda s: s["tools"]["check_permission"].update(description="Old."))
RETIRED = snapshot(lambda s: s["tools"].update(retired=s["tools"]["check_permission"]))


@pytest.mark.parametrize(
    ("base", "status", "says", "warns"),
    [
        (snapshot(), 0, "surface_diff: 0 changes, 0 breaking", []),
        (REWORDED, 0, "non-breaking  tool check_permission: description changed", []),
        (RETIRED, 0, "BREAKING      tool retired: removed", [BREAKING]),
        (None, 0, "No base snapshot: main (HEAD^1) has no tests/snapshots/surface.json", []),
        ({"not": "a snapshot"}, 2, "### Tool surface changes against main", [CANNOT_COMPARE]),
    ],
    ids=["unchanged", "non-breaking", "breaking", "no base snapshot", "cannot compare"],
)  # fmt: skip
def test_the_surface_report_lists_each_change_and_warns_of_breaking_ones(
    tmp_path: Path, base: object, status: int, says: str, warns: list[str]
) -> None:
    completed, summary = surface(tmp_path, base)
    assert completed.returncode == status, completed.stdout + completed.stderr
    assert says in summary
    assert workflow_commands(completed) == warns


def test_the_surface_report_compares_with_the_merged_base_not_the_moving_tip(
    tmp_path: Path,
) -> None:
    later = snapshot(lambda s: s["tools"].update(added_later=s["tools"]["check_permission"]))
    completed, summary = surface(tmp_path, snapshot(), later_base=later)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert summary.startswith("### Tool surface changes against main (HEAD^1)\n```\n")
    assert "surface_diff: 0 changes, 0 breaking" in summary


# --- ci-steps.sh smoke and mutation ----------------------------------------------------------


@pytest.mark.parametrize(
    ("exit_code", "message", "status"),
    [(2, "configuration error: no key", 0), (0, "configuration error: no key", 1), (2, "Oops", 1)],
    ids=["a configuration error", "exit 0", "another error"],
)
def test_the_console_script_must_exit_2_on_a_missing_configuration(
    tmp_path: Path, exit_code: int, message: str, status: int
) -> None:
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "permit_mcp-1.0.0-py3-none-any.whl").write_text("")
    permit_mcp = f"#!/bin/sh\necho '{message}' >&2\nexit {exit_code}\n"
    stand_in(
        tmp_path,
        "uv",
        f'echo "$*" >>"$RUNNER_TEMP/uv.log"\n[[ $1 == venv ]] || exit 0\n'
        f'mkdir -p "$3/bin"\nprintf %s "{permit_mcp}" >"$3/bin/permit-mcp"\n'
        'chmod +x "$3/bin/permit-mcp"',
    )
    completed = run_script(tmp_path, "ci-steps.sh", "smoke", tmp_path / "dist")
    assert completed.returncode == status, completed.stdout + completed.stderr
    assert (tmp_path / "uv.log").read_text().splitlines()[1] == (
        f"pip install --quiet --python {tmp_path}/smoke/bin/python --exclude-newer false"
        f" {tmp_path}/dist/permit_mcp-1.0.0-py3-none-any.whl"
    )
    if status:
        assert f"permit-mcp with no configuration exited {exit_code}" in completed.stdout


TOO_FEW = "::error title=Mutation tests::The tests catch too few mutants of the changed lines;"
DID_NOT_RUN = "::error title=Mutation tests::The mutation tests did not run; see the log."


@pytest.mark.parametrize(
    ("gate_status", "status", "says"),
    [
        (0, 0, []),
        (1, 1, [f"{TOO_FEW} see the job summary."]),
        (2, 2, [DID_NOT_RUN]),
        (137, 2, [DID_NOT_RUN]),
    ],
)
def test_the_mutation_gate_runs_on_the_merge_base_and_keeps_its_exit(
    tmp_path: Path, gate_status: int, status: int, says: list[str]
) -> None:
    stand_in(tmp_path, "uv", f'echo "$*" >"$RUNNER_TEMP/uv.log"\nexit {gate_status}')
    completed = run_script(tmp_path, "ci-steps.sh", "mutation")
    assert completed.returncode == status
    assert workflow_commands(completed) == says
    assert (tmp_path / "uv.log").read_text() == (
        "run --locked python .github/scripts/mutation_gate.py --base HEAD^1 --threshold 80"
        " --workers 4 --memory-mib 3072 --max-minutes 25\n"
    )


# --- audit-report.sh ---------------------------------------------------------------------


def plant_reports(tmp_path: Path, vulnerable: str = "", absent: str = "") -> dict[str, str]:
    """Plant Trivy reports of one package per tree, with a fixable HIGH advisory in `vulnerable`.

    Returns the environment audit-report.sh reads them with.
    """
    for tree in TREES:
        if tree == absent:
            continue
        result: dict[str, Any] = {"Target": "x", "Packages": [{"Name": "p", "Version": "1"}]}
        if tree == vulnerable:
            advisory = {"VulnerabilityID": "CVE-1", "PkgName": "p", "Severity": "HIGH"}
            result["Vulnerabilities"] = [{**advisory, "FixedVersion": "2"}]
        (tmp_path / "audit" / tree).mkdir(parents=True)
        (tmp_path / "audit" / tree / "requirements.txt").write_text("p==1\n")
        (tmp_path / "audit" / f"trivy-{tree}.json").write_text(json.dumps({"Results": [result]}))
    python_uv(tmp_path)
    return {"AUDIT_DIR": str(tmp_path / "audit"), "AUDIT_TREES": " ".join(TREES)}


AUDIT_ERROR = "::error title=Dependency audit::"
GATE_SAYS = {
    0: [],
    1: [f"{AUDIT_ERROR}Fixable HIGH or CRITICAL advisories; see the job summary."],
    2: [f"{AUDIT_ERROR}The audit did not complete; see the job summary."],
}


@pytest.mark.parametrize(
    ("vulnerable", "absent", "gate_dev_tree", "status"),
    [
        ("", "", "true", 0),
        ("runtime-floor", "", "null", 1),
        ("dev-ceiling", "", "true", 1),
        ("docs-ceiling", "", "null", 1),
        ("example-ceiling", "", "true", 1),
        ("dev-ceiling", "", "false", 0),
        ("docs-ceiling", "", "false", 0),
        ("example-ceiling", "", "false", 0),
        ("runtime-ceiling", "", "false", 1),
        ("runtime-floor", "", "false", 1),
        ("", "dev-ceiling", "true", 2),
        ("", "runtime-floor", "false", 2),
    ],
)
def test_the_audit_gate_gates_every_tree_unless_a_caller_ungates_the_tool_trees(
    tmp_path: Path, vulnerable: str, absent: str, gate_dev_tree: str, status: int
) -> None:
    env = plant_reports(tmp_path, vulnerable, absent) | {"GATE_DEV_TREE": gate_dev_tree}
    completed = run_script(tmp_path, "audit-report.sh", "gate", env=env)
    assert completed.returncode == status, completed.stdout + completed.stderr
    lines = workflow_commands(completed)
    blocking = [line for line in lines if line.startswith("::error title=HIGH: CVE-1 in p::")]
    assert len(blocking) == (status == 1), "each blocking advisory is annotated"
    assert [line for line in lines if line not in blocking] == GATE_SAYS[status]
    ungated = "dev-ceiling docs-ceiling example-ceiling: reported in the summary but not gated"
    assert (ungated in completed.stdout) == (gate_dev_tree == "false")


def test_the_audit_summary_reads_every_tree(tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"
    env = plant_reports(tmp_path, "dev-ceiling") | {"GITHUB_STEP_SUMMARY": str(summary)}
    completed = run_script(tmp_path, "audit-report.sh", "summary", "the planted trees", env=env)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "the planted trees" in summary.read_text()
    assert "CVE-1" in summary.read_text()


@pytest.mark.parametrize(
    ("result", "line"),
    [
        ("success", ">e2e: success"),
        ("failure", ">e2e: failure (did not run, or a test failed)"),
        ("cancelled", ">e2e: cancelled (did not run to the end)"),
        ("skipped", ">e2e: skipped (did not run)"),
    ],
)
def test_the_slack_message_carries_the_ci_and_e2e_results(
    tmp_path: Path, result: str, line: str
) -> None:
    output = tmp_path / "output"
    env = plant_reports(tmp_path) | {"GITHUB_OUTPUT": str(output), "REPO": "o/r"}
    env |= {"RUN_URL": "https://example.invalid/run", "CI_RESULT": "success", "E2E_RESULT": result}
    completed = run_script(tmp_path, "audit-report.sh", "slack", env=env)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    text = output.read_text().splitlines()
    assert text[0].startswith("text<<EOF_")
    assert text[-1] == text[0].removeprefix("text<<")
    assert text[-4:-1] == [">CI: success", line, "><https://example.invalid/run|View the run>"]


# --- audit-deps.sh -----------------------------------------------------------------------


def audit_deps(tmp_path: Path, **trees: str) -> subprocess.CompletedProcess[str]:
    """Run audit-deps.sh with a planted uv and trivy that log their calls.

    The planted compile writes the tree its arguments name: the project's floors at
    `--resolution lowest-direct`, above them otherwise, with a marker package in the dev,
    docs and example trees. `trees` plants another tree in place of one, such as
    floor="ceiling". `uv run` runs the real check_floors.py.
    """
    fillers = [f"filler{index}==1.0" for index in range(10)]
    planted = floor_and_ceiling()
    planted |= {
        group: [*planted["ceiling"], marker]
        for group, marker in [
            ("dev", "pytest==9.1.1"),
            ("docs", "zensical==0.0.65"),
            ("example", "google-genai==2.25.0"),
        ]
    }
    for name in planted:
        pins = planted[trees.get(name, name)]
        (tmp_path / f"{name}.txt").write_text("\n".join([*pins, *fillers]) + "\n")
    python_uv(
        tmp_path,
        f"""tree={tmp_path}/ceiling.txt
while [[ $# -gt 0 ]]; do
  case $1 in
    -o) out=$2; shift ;;
    --resolution) [[ $2 == lowest-direct ]] && tree={tmp_path}/floor.txt; shift ;;
    --group) tree={tmp_path}/dev.txt; [[ $2 == *:docs ]] && tree={tmp_path}/docs.txt; shift ;;
    */examples/food-ordering-system/pyproject.toml) tree={tmp_path}/example.txt ;;
  esac
  shift
done
cp "$tree" "$out\"""",
    )
    stand_in(
        tmp_path,
        "trivy",
        'echo "trivy $*" >>"$RUNNER_TEMP/uv.log"\n'
        'while [[ $1 != --output ]]; do shift; done\necho \'{"Results": []}\' >"$2"',
    )
    return run_script(tmp_path, "audit-deps.sh", tmp_path / "audit")


def test_audit_deps_compiles_and_scans_each_tree_as_it_says(tmp_path: Path) -> None:
    completed = audit_deps(tmp_path)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Every direct dependency is at its floor" in completed.stdout
    calls = (tmp_path / "uv.log").read_text().splitlines()
    out = f"--python-version 3.11 --quiet -o {tmp_path}/audit"
    published = f"--no-sources --exclude-newer false {out}"
    project = f"{REPO_ROOT}/pyproject.toml"
    assert [call for call in calls if call.startswith("pip compile")] == [
        f"pip compile {project} {published}/runtime-ceiling/requirements.txt",
        (
            f"pip compile {project} {published}/runtime-floor/requirements.txt"
            " --resolution lowest-direct"
        ),
        f"pip compile {project} --group {project}:dev {published}/dev-ceiling/requirements.txt",
        f"pip compile {project} --group {project}:docs {published}/docs-ceiling/requirements.txt",
        # The example installs permit-mcp from this checkout, so its sources apply.
        (
            f"pip compile {REPO_ROOT}/examples/food-ordering-system/pyproject.toml"
            f" --exclude-newer false {out}/example-ceiling/requirements.txt"
        ),
    ]
    # Trivy reads no trivy.yaml or .trivyignore from the checkout, and every severity.
    assert [call for call in calls if call.startswith("trivy ")] == [
        "trivy fs --config /dev/null --scanners vuln --severity UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL"
        " --list-all-pkgs --format json --ignorefile /dev/null"
        f" --output {tmp_path}/audit/trivy-{tree}.json --quiet {tmp_path}/audit/{tree}"
        for tree in TREES
    ]


@pytest.mark.parametrize(
    ("tree", "says"),
    [
        ("floor", "its floor is"),
        ("dev", "Tree 'dev-ceiling' has no pytest"),
        ("docs", "Tree 'docs-ceiling' has no zensical"),
        ("example", "Tree 'example-ceiling' has no google-genai"),
    ],
)
def test_audit_deps_fails_a_tree_that_is_not_what_it_says(
    tmp_path: Path, tree: str, says: str
) -> None:
    completed = audit_deps(tmp_path, **{tree: "ceiling"})
    assert completed.returncode == 1
    assert says in completed.stdout


def pinned_version(url: str) -> str:
    match = re.search(r"/download/v([0-9.]+)/", read_workflow("ci.yml")["env"][url])
    assert match is not None
    return match.group(1)


def test_a_planted_trivy_config_and_ignore_file_do_not_hide_advisories(tmp_path: Path) -> None:
    # The pinned Trivy, on the real audit-deps.sh, in a planted repository whose
    # trivy.yaml asks for LOW only and whose .trivyignore lists the advisories.
    trivy = tool("trivy")
    version = subprocess.run([trivy, "--version"], capture_output=True, text=True, check=True)
    assert f"Version: {pinned_version('TRIVY_URL')}" in version.stdout
    repo = tmp_path / "repo"
    (repo / ".github" / "scripts").mkdir(parents=True)
    for script in ("audit-deps.sh", "check_floors.py"):
        shutil.copy(SCRIPTS / script, repo / ".github" / "scripts" / script)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "planted"\nversion = "0"\nrequires-python = ">=3.11"\n'
        'dependencies = ["aiohttp>=3.9.1,<3.9.2", "requests>=2.31.0,<2.31.1"]\n'
        '[dependency-groups]\ndev = ["pytest==9.1.1"]\ndocs = ["zensical==0.0.65"]\n'
    )
    (repo / "examples" / "food-ordering-system").mkdir(parents=True)
    (repo / "examples" / "food-ordering-system" / "pyproject.toml").write_text(
        '[project]\nname = "planted-example"\nversion = "0"\nrequires-python = ">=3.11"\n'
        'dependencies = ["google-genai==2.25.0"]\n'
    )
    (repo / "trivy.yaml").write_text("severity:\n  - LOW\n")
    (repo / ".trivyignore").write_text("CVE-2024-23334\nCVE-2024-30251\nCVE-2025-69223\n")
    out = repo / "audit"
    completed = subprocess.run(
        [tool("bash"), str(repo / ".github" / "scripts" / "audit-deps.sh"), str(out)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    gate = subprocess.run(
        [sys.executable, SCRIPTS / "format_audit.py", "--dir", out, *TREES, "--gate"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert gate.returncode == 1, gate.stderr
    assert "HIGH CVE-2024-23334 aiohttp 3.9.1" in gate.stderr
    # Without --config and --ignorefile, the planted files hide every HIGH one.
    hidden = subprocess.run(
        [trivy, "fs", "--scanners", "vuln", "--format", "json", "--quiet", out / "runtime-floor"],
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


# --- gitleaks-scan.sh --------------------------------------------------------------------

# What gitleaks 8.30.1 prints with --no-banner --no-color, its commit count read as 0
# as a git log format can make it.
CLEAN_SCAN = """8:14PM INF 0 commits scanned.
8:14PM INF scanned ~740369 bytes (740.37 KB) in 117ms
8:14PM INF no leaks found"""
LEAKY_SCAN = """Finding:     token = REDACTED
RuleID:      github-pat
File:        token.txt

8:14PM INF 0 commits scanned.
8:14PM INF scanned ~119 bytes (119 bytes) in 83.1ms
8:14PM WRN leaks found: 1"""
ACCEPT_FILE = ".github/gitleaks-accept.txt"
BASE_ENTRY = "a" * 40 + ":base.txt:github-pat:1"
PULL_REQUEST_ENTRY = "b" * 40 + ":pr.txt:github-pat:1"


def scan(
    tmp_path: Path, checkout: Path = REPO_ROOT, event: str = "push", output: str = CLEAN_SCAN
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the scan of `checkout` on `event` with a planted gitleaks that prints `output`.

    It exits 0 when `output` ends as a clean scan does, and 1 otherwise. Returns what
    the scan accepted, too.
    """
    exit_code = 0 if output.endswith("no leaks found") else 1
    gitleaks = f'printf "%s\\n" "$@" >"$RUNNER_TEMP/args"\ncat >&2 <<\'OUT\'\n{output}\nOUT'
    stand_in(tmp_path, "gitleaks", f"{gitleaks}\nexit {exit_code}")
    completed = run_script(
        tmp_path, "gitleaks-scan.sh", tmp_path / "bin/gitleaks", env={"EVENT": event}, cwd=checkout
    )
    ignore = tmp_path / "gitleaks-ignores" / ".gitleaksignore"
    return completed, ignore.read_text().split() if ignore.exists() else []


def git_answering(tmp_path: Path, shallow: str = "false", commits: str = "14") -> None:
    """Plant a git that says whether the checkout is shallow and how many commits it has.

    It skips the mirror clone, and passes the rest on.
    """
    stand_in(
        tmp_path,
        "git",
        f'case "$1 $2" in\n  "rev-parse --is-shallow-repository") echo {shallow} ;;\n'
        f'  clone*) ;;\n  *) if [[ " $* " == *" rev-list "* ]]; then echo {commits};'
        f' else exec "{tool("git")}" "$@"; fi ;;\nesac',
    )


@pytest.mark.parametrize(
    ("output", "answers", "status", "says"),
    [
        (CLEAN_SCAN, "false 14", 0, "scanned 740369 bytes of 14 commits and found no secret"),
        (LEAKY_SCAN, "false 14", 1, "::error title=gitleaks::Secrets found in the history"),
        (CLEAN_SCAN.replace("~740369", "~0"), "false 14", 2, "gitleaks scanned nothing"),
        ("8:14PM FTL could not run git log", "false 14", 2, "gitleaks scanned nothing"),
        (CLEAN_SCAN, "true 14", 2, "The checkout is shallow"),
        (CLEAN_SCAN, "false 0", 2, "holds no commit"),
    ],
    ids=["clean", "a leak", "no bytes", "no count", "shallow", "no commit"],
)
def test_the_scan_proves_it_ran(
    tmp_path: Path, output: str, answers: str, status: int, says: str
) -> None:
    git_answering(tmp_path, *answers.split())
    completed, _ = scan(tmp_path, output=output)
    assert completed.returncode == status, completed.stdout + completed.stderr
    assert says in completed.stdout


def test_the_scan_reads_no_configuration_from_the_checkout(tmp_path: Path) -> None:
    git_answering(tmp_path)
    completed, accepted = scan(tmp_path)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    args = (tmp_path / "args").read_text().splitlines()
    assert args[0] == "git"
    assert args[args.index("--config") + 1] == f"{tmp_path}/gitleaks.toml"
    assert (tmp_path / "gitleaks.toml").read_text() == "[extend]\nuseDefault = true\n"
    ignore_path = Path(args[args.index("--gitleaks-ignore-path") + 1])
    assert [entry.name for entry in ignore_path.iterdir()] == [".gitleaksignore"]
    listed = (REPO_ROOT / ACCEPT_FILE).read_text().splitlines()
    assert accepted == [line.split()[0] for line in listed if line and not line.startswith("#")]
    assert {"--ignore-gitleaks-allow", "--redact", "--exit-code"} <= set(args)
    assert args[-1] == f"{tmp_path}/history.git", "the scan reads the mirror, not the checkout"


def commit_accept_list(repo: Path, text: str | None, message: str) -> None:
    """Commit `text` as the accept list: None commits none, and "" deletes it."""
    (repo / ".github").mkdir(exist_ok=True)
    (repo / "other.txt").write_text(message)
    if text == "":
        git(repo, "rm", "--quiet", ACCEPT_FILE)
    elif text is not None:
        (repo / ACCEPT_FILE).write_text(text)
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", message)


def merged_pull_request(tmp_path: Path, base: str | None, pull_request: str | None) -> Path:
    """A checkout of a pull request's merge commit, as actions/checkout makes on pull_request.

    `base` and `pull_request` are the accept list on each side, as commit_accept_list takes.
    """
    repo = tmp_path / "checkout"
    repo.mkdir()
    git(repo, "init", "--quiet", "--initial-branch=main")
    commit_accept_list(repo, base, "base")
    git(repo, "checkout", "--quiet", "-b", "pr")
    commit_accept_list(repo, pull_request, "pull request")
    git(repo, "checkout", "--quiet", "main")
    git(repo, "merge", "--quiet", "--no-ff", "-m", "merge", "pr")
    return repo


BASE_LIST = f"{BASE_ENTRY}  # reviewed on main\n"
BOTH_LISTS = f"{BASE_LIST}{PULL_REQUEST_ENTRY}  # mine\n"
FIRST_LIST = f"{PULL_REQUEST_ENTRY}  # the first list\n"
OWN_LIST = (
    "::warning title=gitleaks::The base (HEAD^1) has no .github/gitleaks-accept.txt, so the"
    " pull request's own list is read. Review every entry in it."
)


@pytest.mark.parametrize(
    ("base", "pull_request", "event", "says"),
    [
        (BASE_LIST, BOTH_LISTS, "pull_request", [BASE_ENTRY]),
        (BASE_LIST, "", "pull_request", [BASE_ENTRY]),
        (None, FIRST_LIST, "pull_request", [OWN_LIST, PULL_REQUEST_ENTRY]),
        (None, None, "pull_request", []),
        (BASE_LIST, BOTH_LISTS, "push", [BASE_ENTRY, PULL_REQUEST_ENTRY]),
        (BASE_LIST, BOTH_LISTS, "schedule", [BASE_ENTRY, PULL_REQUEST_ENTRY]),
        (BASE_LIST, BOTH_LISTS, "workflow_dispatch", [BASE_ENTRY, PULL_REQUEST_ENTRY]),
    ],
    ids=["from the base", "deleted", "the first list", "none", "push", "schedule", "dispatch"],
)
def test_a_pull_request_reads_the_accept_list_from_its_base(
    tmp_path: Path, base: str | None, pull_request: str | None, event: str, says: list[str]
) -> None:
    repo = merged_pull_request(tmp_path, base, pull_request)
    (repo / ".github").mkdir(exist_ok=True)
    (repo / ACCEPT_FILE).write_text("not committed, so not read\n")
    completed, accepted = scan(tmp_path, repo, event)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert accepted == [entry for entry in says if entry != OWN_LIST]
    notice = "::notice title=gitleaks::Accepted as a false positive: "
    expected = [entry if entry == OWN_LIST else notice + entry for entry in says]
    assert [line.split("  #")[0] for line in workflow_commands(completed)] == expected


def test_a_pull_request_whose_head_has_no_parent_exits_2(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git(checkout, "init", "--quiet")
    commit_accept_list(checkout, f"{PULL_REQUEST_ENTRY}  # mine\n", "accept")
    completed, accepted = scan(tmp_path, checkout, "pull_request")
    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert "::error title=gitleaks::HEAD has no parent" in completed.stdout
    assert accepted == []


@pytest.mark.parametrize(
    "line",
    [BASE_ENTRY, f"{BASE_ENTRY}  #", "tests/x.py:generic-api-key:3  # no commit", "*  # all"],
    ids=["no reason", "empty reason", "no commit", "wildcard"],
)
def test_an_accept_line_without_a_fingerprint_and_a_reason_exits_2(
    tmp_path: Path, line: str
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git(checkout, "init", "--quiet")
    commit_accept_list(checkout, f"# comment\n\n{line}\n", "accept")
    completed, _ = scan(tmp_path, checkout)
    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert (
        "::error title=gitleaks::HEAD:.github/gitleaks-accept.txt:3 is not a fingerprint"
        " and a reason."
    ) in completed.stdout


def commit_token(repo: Path, name: str, comment: str = "") -> str:
    """Commit a planted GitHub token in `name`; return its gitleaks fingerprint."""
    alphabet = string.ascii_letters + string.digits
    value = "ghp" + "_" + "".join(secrets.choice(alphabet) for _ in range(36))
    (repo / name).write_text(f"token = {value}{comment}\n")
    git(repo, "add", name)
    git(repo, "commit", "--quiet", "-m", f"plant {name}")
    return f"{git(repo, 'rev-parse', 'HEAD')}:{name}:github-pat:1"


def real_scan(tmp_path: Path, repo: Path, event: str = "push") -> subprocess.CompletedProcess[str]:
    """Run the scan with the pinned gitleaks, in a RUNNER_TEMP of its own."""
    gitleaks = tool("gitleaks")
    version = subprocess.run([gitleaks, "version"], capture_output=True, text=True, check=True)
    assert version.stdout.strip() == pinned_version("GITLEAKS_URL")
    runner_temp = tmp_path / f"run-{len(list(tmp_path.glob('run-*')))}"
    runner_temp.mkdir()
    return run_script(runner_temp, "gitleaks-scan.sh", gitleaks, env={"EVENT": event}, cwd=repo)


def test_the_pinned_gitleaks_finds_a_token_despite_the_repository_s_suppressions(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--quiet")
    commit_accept_list(repo, None, "clean")
    clean = real_scan(tmp_path, repo)
    assert clean.returncode == 0, clean.stdout + clean.stderr
    assert "of 1 commits and found no secret" in clean.stdout
    fingerprint = commit_token(repo, "token.txt", "  # gitleaks:allow")
    (repo / ".gitleaks.toml").write_text(
        "[extend]\nuseDefault = true\n\n[allowlist]\npaths = ['''token\\.txt''']\n"
    )
    (repo / ".gitleaksignore").write_text(f"{fingerprint}\n")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "suppress")
    # The suppressions work when gitleaks reads them from the repository...
    default = subprocess.run(
        [tool("gitleaks"), "git", "--no-banner", "--exit-code", "1", "."],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert default.returncode == 0, default.stdout + default.stderr
    # ...and the scan reads none of them.
    completed = real_scan(tmp_path, repo)
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "RuleID:      github-pat" in completed.stdout
    assert "ghp_" not in completed.stdout, "findings are redacted"


def test_an_accepted_fingerprint_hides_only_its_own_finding_and_only_once_merged(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    git(repo, "init", "--quiet", "--initial-branch=main")
    token_a = commit_token(repo, "a.txt")
    commit_accept_list(repo, f"{token_a}  # reviewed on main\n", "accept a")
    accepted_only = real_scan(tmp_path, repo)
    assert accepted_only.returncode == 0, accepted_only.stdout + accepted_only.stderr
    git(repo, "checkout", "--quiet", "-b", "pr")
    token_b = commit_token(repo, "b.txt")
    commit_accept_list(repo, f"{token_a}  # reviewed on main\n{token_b}  # mine\n", "accept b")
    git(repo, "checkout", "--quiet", "main")
    git(repo, "merge", "--quiet", "--no-ff", "-m", "merge", "pr")
    completed = real_scan(tmp_path, repo, "pull_request")
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "File:        b.txt" in completed.stdout
    assert "File:        a.txt" not in completed.stdout
    assert f"Accepted as a false positive: {token_a}" in completed.stdout
    assert f"Accepted as a false positive: {token_b}" not in completed.stdout
    # Once merged, the entry is the base's and counts.
    merged = real_scan(tmp_path, repo, "push")
    assert merged.returncode == 0, merged.stdout + merged.stderr


# --- build-docs.sh -----------------------------------------------------------------------

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
COLOURED = "\x1b[1mBuild started\x1b[0m\nNo issues found\nBuild finished in 12ms\n"
LINK = "<a href='x/'>x</a>"


def build_docs(tmp_path: Path, zensical: str) -> subprocess.CompletedProcess[str]:
    """Run build-docs.sh in a planted one-page checkout, with `zensical` as uv.

    `uv run --no-project ... python` runs the real check_site.py instead.
    """
    (tmp_path / ".github" / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPTS / "check_site.py", tmp_path / ".github" / "scripts" / "check_site.py")
    (tmp_path / "mkdocs.yml").write_text(
        "site_url: https://example.github.io/planted/\n\nnav:\n  - Home: index.md\n\nplugins: []\n"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "index.md").write_text("# Home\n")
    python_uv(tmp_path, zensical)
    return run_script(tmp_path, "build-docs.sh", cwd=tmp_path)


@pytest.mark.parametrize(
    ("log", "html", "status", "says"),
    [
        (CLEAN_BUILD, LINK, 0, "The docs built with no warning."),
        (COLOURED, LINK, 0, "Build started\nNo issues found\n"),
        (GRIFFE_WARNING, LINK, 1, "griffe: src/permit_mcp/identity.py:43: Failed to get"),
        (CLEAN_BUILD + "WARNING -  mkdocs_autorefs: no target\n", LINK, 1, "mkdocs_autorefs"),
        (CLEAN_BUILD + "Warning: anchor does not exist\n", LINK, 1, "anchor does not exist"),
        (CLEAN_BUILD + "A line a clean build does not print\n", LINK, 1, "A line a clean"),
        (CLEAN_BUILD, None, 1, "nav lists index.md, but the build wrote no site/index.html"),
        (CLEAN_BUILD, "<a href='/r/'>r</a>", 1, "/r/ starts with / outside the site's path"),
        (UNRESOLVED_REFERENCE, LINK, 1, "::error title=Docs::zensical build exited 1"),
        ("Build started\n", LINK, 2, "::error title=Docs::The docs build did not finish."),
    ],
    ids=[
        "clean", "coloured", "griffe", "autorefs", "anchor", "any other line", "a nav page",
        "a root link", "strict stops it", "unfinished",
    ],
)  # fmt: skip
def test_build_docs_fails_on_any_line_a_clean_build_does_not_print_and_what_it_did_not_build(
    tmp_path: Path, log: str, html: str | None, status: int, says: str
) -> None:
    # Zensical exits 1 when --strict stops the build, and 0 on any other log.
    zensical_status = int("--strict flag is set" in log)
    (tmp_path / "zensical.log").write_text(log)
    (tmp_path / "page.html").write_text(html or "")
    write_site = (
        "" if html is None else f'mkdir -p site && cp "{tmp_path}/page.html" site/index.html\n'
    )
    completed = build_docs(
        tmp_path,
        'if [[ " $* " != *" zensical "* ]]; then exit 0; fi\n'
        f'{write_site}cat "{tmp_path}/zensical.log"\nexit {zensical_status}',
    )
    assert completed.returncode == status, completed.stdout + completed.stderr
    assert says in completed.stdout
    assert "\x1b" not in completed.stdout
    if "::error title=Docs::The docs build printed" in completed.stdout:
        repeated = completed.stdout.split("::error title=Docs::", 1)[1]
        assert says in repeated
        assert "Build started" not in repeated, "only the unexpected lines are repeated"
    assert (tmp_path / "uv.log").read_text().splitlines() == [
        "run --locked --group docs python scripts/docs_pages.py",
        "run --locked --group docs zensical build --strict --clean",
    ]


def test_build_docs_names_a_missing_snippet_when_the_build_also_failed(tmp_path: Path) -> None:
    (tmp_path / "snippet.md").write_text('--8<-- "GONE.md"\n')
    completed = build_docs(
        tmp_path,
        f'if [[ " $* " == *" zensical "* ]]; then cp "{tmp_path}/snippet.md" docs/index.md;'
        " exit 1; fi",
    )
    assert completed.returncode == 1
    assert "docs/index.md includes GONE.md, which is missing" in completed.stdout


def test_build_docs_stops_when_the_pages_cannot_be_written(tmp_path: Path) -> None:
    completed = build_docs(tmp_path, "exit 3")
    assert completed.returncode == 3
    assert len((tmp_path / "uv.log").read_text().splitlines()) == 1
