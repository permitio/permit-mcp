"""Pins the facts of ci.yml, release.yml and pages.yml that actionlint and zizmor do not check.

When each workflow, job and step runs (triggers, `if:`s, needs, timeouts), what each
step that runs a command runs and with which inputs, the tests matrix, that nothing
may fail but notify's artifact download, the concurrency groups, that release.yml
grants the workflows it calls what their jobs ask for, and the tools pinned in more
than one place. What each gate decides is tested on its script, in the other files.

Run with:
uv run --only-dev pytest -c .github/scripts/pytest.ini .github/scripts/test_workflows.py
"""

from __future__ import annotations

import copy
import re
import textwrap
import tomllib
from typing import Any

import pytest

from harness import REPO_ROOT, SCRIPTS, read_workflow

OWN_RUN = (
    "github.workflow == 'CI' &&"
    " (github.event_name == 'schedule' || github.event_name == 'workflow_dispatch')"
)
PULL_REQUEST = "github.event_name == 'pull_request'"
NEEDED = [
    "audit",
    "audit-scripts",
    "dependency-review",
    "docs",
    "e2e",
    "e2e-pdp-latest",
    "example",
    "gitleaks",
    "mutation",
    "package",
    "prek",
    "tests",
    "workflow-hardening",
]

# workflow: its triggers, and per job its if:, needs, timeout and checkout inputs
# (but persist-credentials, which zizmor checks; None: no checkout).
JOBS: dict[str, tuple[list[str], dict[str, tuple[Any, ...]]]] = {
    "ci.yml": (
        ["pull_request", "push", "schedule", "workflow_dispatch", "workflow_call"],
        {
            "prek": (None, None, 15, {}),
            "tests": (None, None, 15, {"fetch-depth": 2}),
            "package": (None, None, 10, {}),
            "docs": (None, None, 10, {}),
            "example": (None, None, 10, {}),
            "audit": (None, None, 15, {}),
            "audit-scripts": (None, None, 15, {}),
            "dependency-review": (PULL_REQUEST, None, 5, None),
            "mutation": (PULL_REQUEST, None, 30, {"fetch-depth": 2}),
            "workflow-hardening": (None, None, 10, {}),
            "gitleaks": (None, None, 10, {"fetch-depth": 0}),
            "e2e": (OWN_RUN, None, 25, {}),
            "e2e-pdp-latest": (OWN_RUN, None, 25, {}),
            "ci": ("always()", NEEDED, 5, {}),
            "notify": (f"always() && {OWN_RUN}", ["audit", "ci", "e2e", "e2e-pdp-latest"], 5, {}),
        },
    ),
    "release.yml": (
        ["release", "workflow_dispatch"],
        {
            "tag": (None, None, 5, {"fetch-depth": 0}),
            "ci": (None, "tag", None, None),
            "build": (None, "ci", 10, {}),
            "scan": (None, "build", 15, {}),
            "publish": ("github.event_name == 'release'", "scan", 10, {}),
            "docs": (None, "publish", None, None),
        },
    ),
    "pages.yml": (
        ["workflow_call", "workflow_dispatch"],
        {"build": (None, None, 10, {}), "deploy": (None, "build", 10, None)},
    ),
}

# Each step that runs a command or has an if:, per job, in order: its name or action,
# then its if:, each variable of its env: and its command, whitespace collapsed and
# wrapped as render_steps wraps them.
STEPS = r"""
ci.yml prek
  Check that CI needs every job
    run: .github/scripts/ci-steps.sh needs
  Run hooks
    env EXPECTED_HOOKS: 21
    run: .github/scripts/ci-steps.sh hooks
ci.yml tests
  Install from the ranges in pyproject.toml
    if: matrix.resolution != 'locked'
    env RESOLUTION: ${{ matrix.resolution }}
    run: .github/scripts/ci-steps.sh install
  Install from uv.lock
    if: matrix.resolution == 'locked'
    run: uv sync --locked
  Show installed packages
    run: uv pip list
  Import the package with warnings as errors
    run: python -W error -c "import permit_mcp"
  Offline tests
    env PERMIT_MCP_API_RECORD: ${{ matrix.resolution == 'locked' &&
      format('{0}/api-record.jsonl', runner.temp) || '' }}
    run: python -m pytest -q -W error --junitxml="$RUNNER_TEMP/junit.xml"
  Check that every test ran
    run: uv run --no-project --python 3.11 python .github/scripts/check_junit.py
      "$RUNNER_TEMP/junit.xml"
  Report API coverage
    if: matrix.resolution == 'locked'
    env EVENT: ${{ github.event_name }}
    env WORKFLOW: ${{ github.workflow }}
    run: .github/scripts/ci-steps.sh coverage
  actions/upload-artifact
    if: ${{ !cancelled() && matrix.resolution == 'locked' && github.workflow == 'CI' &&
      (github.event_name == 'schedule' || github.event_name == 'workflow_dispatch') }}
  Report tool surface changes
    if: matrix.resolution == 'locked' && github.event_name == 'pull_request'
    env BASE_REF: ${{ github.base_ref }}
    run: .github/scripts/ci-steps.sh surface
ci.yml package
  Build
    run: uv build --no-sources --out-dir "$RUNNER_TEMP/dist"
  Check the wheel and sdist
    run: uv run --no-project --python 3.11 python .github/scripts/check_dist.py
      "$RUNNER_TEMP/dist" pyproject.toml
  Run the console script from the installed wheel
    run: .github/scripts/ci-steps.sh smoke "$RUNNER_TEMP/dist"
  Type-check the public API as a consumer
    run: uv run --locked --only-dev .github/scripts/check-consumer-types.sh "$RUNNER_TEMP/dist"
ci.yml docs
  Build the docs site
    run: .github/scripts/build-docs.sh
ci.yml example
  Install the example from its uv.lock
    run: uv sync --locked --directory examples/food-ordering-system
  ruff check
    run: uv run --locked --directory examples/food-ordering-system ruff check .
  ruff format
    run: uv run --locked --directory examples/food-ordering-system ruff format --check .
  mypy
    run: uv run --locked --directory examples/food-ordering-system mypy
  Example tests
    run: uv run --locked --directory examples/food-ordering-system python -m pytest -q
      --junitxml="$RUNNER_TEMP/junit.xml"
  Check that every test ran
    run: uv run --no-project --python 3.11 python .github/scripts/check_junit.py
      "$RUNNER_TEMP/junit.xml"
ci.yml audit
  Install Trivy
    run: .github/scripts/fetch-binary.sh "$TRIVY_URL" "$TRIVY_SHA256" trivy "$RUNNER_TEMP/bin"
  Scan the dependency trees
    run: .github/scripts/audit-deps.sh "$AUDIT_DIR"
  Write the job summary
    if: ${{ !cancelled() }}
    run: .github/scripts/audit-report.sh summary "pyproject.toml dependencies and groups, and
      the example, resolved for Python 3.11"
  Gate on fixable HIGH and CRITICAL advisories
    if: ${{ !cancelled() }}
    env GATE_DEV_TREE: ${{ toJSON(inputs.gate-dev-tree) }}
    run: .github/scripts/audit-report.sh gate
  actions/upload-artifact
    if: ${{ !cancelled() }}
ci.yml audit-scripts
  Install gitleaks
    run: .github/scripts/fetch-binary.sh "$GITLEAKS_URL" "$GITLEAKS_SHA256" gitleaks
      "$RUNNER_TEMP/bin"
  Install Trivy
    run: .github/scripts/fetch-binary.sh "$TRIVY_URL" "$TRIVY_SHA256" trivy "$RUNNER_TEMP/bin"
  Run the CI script tests
    run: uv run --locked --only-dev pytest -c .github/scripts/pytest.ini -q .github/scripts
ci.yml mutation
  Install from uv.lock
    run: uv sync --locked
  Run the mutation tests on the changed lines
    run: .github/scripts/ci-steps.sh mutation
ci.yml workflow-hardening
  Install actionlint
    run: .github/scripts/fetch-binary.sh "$ACTIONLINT_URL" "$ACTIONLINT_SHA256" actionlint
      "$RUNNER_TEMP/bin"
  actionlint
    run: "$RUNNER_TEMP/bin/actionlint" -color=false
ci.yml gitleaks
  Install gitleaks
    run: .github/scripts/fetch-binary.sh "$GITLEAKS_URL" "$GITLEAKS_SHA256" gitleaks
      "$RUNNER_TEMP/bin"
  Scan the history
    env EVENT: ${{ github.event_name }}
    run: .github/scripts/gitleaks-scan.sh "$RUNNER_TEMP/bin/gitleaks"
ci.yml e2e
  Install from uv.lock
    run: uv sync --locked
  End-to-end tests
    env PERMIT_E2E_PROJECT_API_KEY: ${{ secrets.PERMIT_E2E_PROJECT_API_KEY }}
    env PERMIT_E2E_PROJECT_ID: ${{ secrets.PERMIT_E2E_PROJECT_ID }}
    env PERMIT_MCP_API_RECORD: ${{ runner.temp }}/e2e-record.jsonl
    run: python -m pytest -q -W error -m e2e -p no:cacheprovider
      --junitxml="$RUNNER_TEMP/junit.xml" tests/e2e
  Check that every test ran
    run: uv run --no-project --python 3.11 python .github/scripts/check_junit.py
      "$RUNNER_TEMP/junit.xml"
  Offline tests, recorded for the coverage report
    if: ${{ !cancelled() }}
    env PERMIT_MCP_API_RECORD: ${{ runner.temp }}/api-record.jsonl
    run: python -m pytest -q -W error
  Report API coverage with the end-to-end record
    if: ${{ !cancelled() }}
    run: .github/scripts/ci-steps.sh e2e-coverage
  actions/upload-artifact
    if: ${{ !cancelled() }}
ci.yml e2e-pdp-latest
  Install from uv.lock
    run: uv sync --locked
  Container PDP tests against the latest PDP
    env PERMIT_E2E_PROJECT_API_KEY: ${{ secrets.PERMIT_E2E_PROJECT_API_KEY }}
    env PERMIT_E2E_PROJECT_ID: ${{ secrets.PERMIT_E2E_PROJECT_ID }}
    env PERMIT_E2E_PDP_IMAGE: permitio/pdp-v2:latest
    run: python -m pytest -q -W error -m e2e -k container_pdp -p no:cacheprovider
      --junitxml="$RUNNER_TEMP/junit.xml" tests/e2e
  Check that every test ran
    run: uv run --no-project --python 3.11 python .github/scripts/check_junit.py
      "$RUNNER_TEMP/junit.xml"
ci.yml ci
  Check the needed jobs
    env NEEDS: ${{ toJSON(needs) }}
    env EVENT: ${{ github.event_name }}
    env EXPECTED_JOBS: 13
    env ADVISORY_JOBS: e2e e2e-pdp-latest
    env PULL_REQUEST_JOBS: dependency-review mutation
    env SCHEDULED_JOBS: e2e e2e-pdp-latest
    env WORKFLOW: ${{ github.workflow }}
    run: .github/scripts/ci-steps.sh results
ci.yml notify
  Warn that the Slack webhook is not set
    if: env.SLACK_WEBHOOK_URL == ''
    run: echo "::warning title=Slack::SLACK_WEBHOOK_URL is not set, so this run's result was not
      posted to Slack."
  actions/checkout
    if: env.SLACK_WEBHOOK_URL != ''
  astral-sh/setup-uv
    if: env.SLACK_WEBHOOK_URL != ''
  actions/download-artifact
    if: env.SLACK_WEBHOOK_URL != ''
  Render the message
    if: env.SLACK_WEBHOOK_URL != ''
    env REPO: ${{ github.repository }}
    env RUN_URL: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{
      github.run_id }}
    env CI_RESULT: ${{ needs.ci.result }}
    env E2E_RESULT: ${{ needs.e2e.result }}
    env E2E_PDP_LATEST_RESULT: ${{ needs.e2e-pdp-latest.result }}
    run: .github/scripts/audit-report.sh slack
  slackapi/slack-github-action
    if: env.SLACK_WEBHOOK_URL != ''
release.yml tag
  Check the tag against the version
    env EVENT: ${{ github.event_name }}
    env TAG: ${{ github.event.release.tag_name }}
    env PRERELEASE: ${{ github.event.release.prerelease }}
    run: .github/scripts/release-checks.sh tag
release.yml build
  Build
    run: uv build --no-sources --out-dir dist
  Check the wheel and sdist
    run: uv run --no-project --python 3.11 python .github/scripts/check_dist.py dist
      pyproject.toml
release.yml scan
  Install Trivy
    run: .github/scripts/fetch-binary.sh "$TRIVY_URL" "$TRIVY_SHA256" trivy "$RUNNER_TEMP/bin"
  Scan the dependency trees
    run: .github/scripts/audit-deps.sh "$AUDIT_DIR"
  Write the job summary
    if: ${{ !cancelled() }}
    run: .github/scripts/audit-report.sh summary "the release's dependency ranges, resolved for
      Python 3.11"
  Gate on fixable HIGH and CRITICAL advisories in the runtime trees
    env GATE_DEV_TREE: False
    run: .github/scripts/audit-report.sh gate
  actions/upload-artifact
    if: ${{ !cancelled() }}
release.yml publish
  Check the files to upload
    env TAG: ${{ github.event.release.tag_name }}
    run: .github/scripts/release-checks.sh files dist
pages.yml build
  Check that the ref is a published release
    env REF_TYPE: ${{ github.ref_type }}
    env TAG: ${{ github.ref_name }}
    env GH_TOKEN: ${{ github.token }}
    env GH_REPO: ${{ github.repository }}
    run: .github/scripts/release-checks.sh ref
  Build the docs site
    run: .github/scripts/build-docs.sh
"""

RUN_GROUP = (
    "${{ (github.event_name == 'schedule' || github.event_name == 'workflow_dispatch')"
    " && github.run_id || 'shared' }}"
)
REF_GROUP = "${{ github.workflow }}-${{ github.ref }}"
PR_CANCELS = "${{ github.event_name == 'pull_request' }}"
# Each job's group and cancel-in-progress; any other ci.yml job has its own group per
# ref, but a run of its own on scheduled and manual runs.
CONCURRENCY: dict[tuple[str, str], tuple[str, str | bool] | None] = {
    ("ci.yml", "tests"): (
        f"{REF_GROUP}-tests-${{{{ matrix.python }}}}-${{{{ matrix.resolution }}}}-{RUN_GROUP}",
        PR_CANCELS,
    ),
    ("ci.yml", "dependency-review"): (f"{REF_GROUP}-dependency-review", PR_CANCELS),
    ("ci.yml", "mutation"): (f"{REF_GROUP}-mutation", PR_CANCELS),
    ("ci.yml", "e2e"): ("${{ github.repository }}-permit-e2e-project", False),
    ("ci.yml", "e2e-pdp-latest"): ("${{ github.repository }}-permit-e2e-project", False),
    ("ci.yml", "notify"): (f"{REF_GROUP}-notify-${{{{ github.run_id }}}}", False),
    ("release.yml", "*"): (REF_GROUP, False),
    ("pages.yml", "deploy"): ("pages", False),
}
# The order of the levels a permission can be granted at.
LEVELS = ("none", "read", "write")


@pytest.fixture(scope="module")
def workflows() -> dict[str, dict[str, Any]]:
    return {name: read_workflow(name) for name in JOBS}


def step_key(step: dict[str, Any]) -> str:
    return str(step.get("name") or step["uses"].split("@")[0])


@pytest.mark.parametrize("name", list(JOBS))
def test_each_job_runs_when_and_as_long_as_it_says(
    workflows: dict[str, dict[str, Any]], name: str
) -> None:
    triggers, jobs = JOBS[name]
    workflow = workflows[name]
    assert list(workflow["on"]) == triggers
    found = {}
    for job_name, job in workflow["jobs"].items():
        steps = job.get("steps", [])
        checkouts = [s["with"] for s in steps if s.get("uses", "").startswith("actions/checkout@")]
        checkout = None
        if checkouts:
            checkout = {k: v for k, v in checkouts[0].items() if k != "persist-credentials"}
        found[job_name] = (job.get("if"), job.get("needs"), job.get("timeout-minutes"), checkout)
    assert found == jobs


def render_steps(name: str, workflow: dict[str, Any]) -> list[str]:
    lines = []
    for job_name, job in workflow["jobs"].items():
        steps = [step for step in job.get("steps", []) if "run" in step or "if" in step]
        lines += [f"{name} {job_name}"] if steps else []
        for step in steps:
            lines.append(f"  {step_key(step)}")
            fields = [("if", step.get("if"))]
            fields += [(f"env {key}", value) for key, value in step.get("env", {}).items()]
            for label, value in [*fields, ("run", step.get("run"))]:
                if value is not None:
                    text = f"{label}: {' '.join(str(value).split())}"
                    lines += textwrap.wrap(
                        text,
                        96,
                        initial_indent="    ",
                        subsequent_indent="      ",
                        break_long_words=False,
                        break_on_hyphens=False,
                    )
    return lines


def test_each_step_runs_when_and_what_it_says(workflows: dict[str, dict[str, Any]]) -> None:
    found = [line for name, workflow in workflows.items() for line in render_steps(name, workflow)]
    assert "\n".join(found) == STEPS.strip()


@pytest.mark.parametrize("name", list(JOBS))
def test_every_step_stops_on_an_error_an_unset_variable_and_a_failed_pipe(
    workflows: dict[str, dict[str, Any]], name: str
) -> None:
    # Each run: is one command too, so no shell can drop a failure between two.
    assert workflows[name]["defaults"] == {
        "run": {"shell": "bash --noprofile --norc -euo pipefail {0}"}
    }


def test_each_action_is_pinned_to_one_commit_named_by_one_version() -> None:
    pins: dict[str, set[tuple[str, str]]] = {}
    for name in JOBS:
        for line in (REPO_ROOT / ".github" / "workflows" / name).read_text().splitlines():
            if re.match(r"\s*(- )?uses: ", line) and "uses: ./" not in line:
                match = re.fullmatch(r"\s*(?:- )?uses: (\S+)@([0-9a-f]{40})  # (v\S+)", line)
                assert match, f"{name}: {line.strip()} is not pinned as owner/repo@sha  # vX"
                pins.setdefault(match[1], set()).add((match[2], match[3]))
    assert {action: len(pairs) for action, pairs in pins.items()} == dict.fromkeys(pins, 1)


def origins_beside(record: str) -> str:
    """The origins file tests/api_record.py writes beside `record`: `<stem>.origins.json`."""
    directory, _, name = record.rpartition("/")
    return f"{directory}/{name.removesuffix('.jsonl')}.origins.json"


def test_the_e2e_job_s_report_reads_the_records_its_steps_write(
    workflows: dict[str, dict[str, Any]],
) -> None:
    steps = {step_key(step): step for step in workflows["ci.yml"]["jobs"]["e2e"]["steps"]}
    e2e = steps["End-to-end tests"]["env"]["PERMIT_MCP_API_RECORD"]
    offline = steps["Offline tests, recorded for the coverage report"]["env"][
        "PERMIT_MCP_API_RECORD"
    ]
    assert e2e == "${{ runner.temp }}/e2e-record.jsonl"
    assert offline == "${{ runner.temp }}/api-record.jsonl"
    script = (SCRIPTS / "ci-steps.sh").read_text()
    (body,) = re.findall(r"^e2e_coverage\(\) \{\n(.*?)^\}", script, flags=re.MULTILINE | re.DOTALL)
    for record in (e2e, offline):
        for path in (record, origins_beside(record)):
            assert path.replace("${{ runner.temp }}", '"$RUNNER_TEMP') + '"' in body, path
    assert '--e2e-record "$RUNNER_TEMP/e2e-record.jsonl"' in body
    assert "--baseline" not in body
    upload = steps["actions/upload-artifact"]["with"]
    assert upload["name"] == "e2e-api-record"
    assert upload["path"].splitlines() == [e2e, origins_beside(e2e)]
    assert "overwrite" not in upload


def test_the_latest_pdp_leg_runs_the_container_tests_on_latest_and_reports_apart(
    workflows: dict[str, dict[str, Any]],
) -> None:
    jobs = workflows["ci.yml"]["jobs"]
    pinned, latest = jobs["e2e"], jobs["e2e-pdp-latest"]
    image_env = [
        step["env"].get("PERMIT_E2E_PDP_IMAGE")
        for job in (pinned, latest)
        for step in job["steps"]
        if "env" in step and "PERMIT_E2E_PROJECT_API_KEY" in step["env"]
    ]
    # The pinned job runs tests/e2e/pdp.py's PDP_IMAGE; only the latest leg names another.
    assert image_env == [None, "permitio/pdp-v2:latest"]
    (run,) = [
        step["run"] for step in latest["steps"] if "PERMIT_E2E_PDP_IMAGE" in step.get("env", {})
    ]
    assert " -m e2e -k container_pdp " in run
    for key in ("if", "environment", "concurrency", "timeout-minutes", "permissions"):
        assert latest[key] == pinned[key], key
    # Separate jobs, not one matrix job, so each has its own result in needs.
    assert "strategy" not in pinned
    assert "strategy" not in latest
    ci_env = next(s for s in jobs["ci"]["steps"] if s.get("name") == "Check the needed jobs")["env"]
    for name in ("ADVISORY_JOBS", "SCHEDULED_JOBS"):
        assert ci_env[name].split() == ["e2e", "e2e-pdp-latest"], name


def test_the_tests_matrix_covers_every_python_and_resolution(
    workflows: dict[str, dict[str, Any]],
) -> None:
    assert workflows["ci.yml"]["jobs"]["tests"]["strategy"] == {
        "fail-fast": False,
        "matrix": {
            "python": ["3.11", "3.12", "3.13", "3.14"],
            "resolution": ["lowest-direct", "highest"],
            "include": [{"python": "3.13", "resolution": "locked"}],
        },
    }


def test_only_notify_s_artifact_download_may_fail(workflows: dict[str, dict[str, Any]]) -> None:
    allowed = []
    for name, workflow in workflows.items():
        for job_name, job in workflow["jobs"].items():
            assert "continue-on-error" not in job, job_name
            allowed += [
                (name, job_name, step_key(step))
                for step in job.get("steps", [])
                if "continue-on-error" in step
            ]
    assert allowed == [("ci.yml", "notify", "actions/download-artifact")]


@pytest.mark.parametrize("name", list(JOBS))
def test_concurrency(workflows: dict[str, dict[str, Any]], name: str) -> None:
    workflow = workflows[name]
    for scope_name, scope in {"*": workflow, **workflow["jobs"]}.items():
        default = None
        if name == "ci.yml" and scope_name != "*":
            default = (f"{REF_GROUP}-{scope_name}-{RUN_GROUP}", PR_CANCELS)
        found = scope.get("concurrency")
        if found is not None:
            found = (" ".join(found["group"].split()), found["cancel-in-progress"])
        assert found == CONCURRENCY.get((name, scope_name), default), (name, scope_name)


def needed_grants(called: dict[str, Any]) -> dict[str, str]:
    """The least grant that covers every permission the called workflow or its jobs ask for.

    GitHub refuses the whole calling run when the calling job grants less, even for a
    called job the run would skip.
    """
    needed: dict[str, str] = {}
    for scope in [called, *called["jobs"].values()]:
        for name, level in (scope.get("permissions") or {}).items():
            if LEVELS.index(level) > LEVELS.index(needed.get(name, "none")):
                needed[name] = level
    return needed


@pytest.mark.parametrize(
    ("job", "called", "grant"),
    [
        ("ci", "ci.yml", {"contents": "read"}),
        ("docs", "pages.yml", {"contents": "read", "pages": "write", "id-token": "write"}),
    ],
)
def test_release_calls_each_workflow_with_exactly_the_grants_its_jobs_need(
    workflows: dict[str, dict[str, Any]], job: str, called: str, grant: dict[str, str]
) -> None:
    calling = workflows["release.yml"]["jobs"][job]
    assert calling["uses"] == f"./.github/workflows/{called}"
    assert "secrets" not in calling, "no secret is passed, and never `secrets: inherit`"
    assert calling["permissions"] == needed_grants(workflows[called]) == grant
    # The release's own CI gates on the runtime trees only; every other run on all.
    assert calling.get("with") == ({"gate-dev-tree": False} if job == "ci" else None)
    declared = workflows["ci.yml"]["on"]["workflow_call"]["inputs"]["gate-dev-tree"]
    assert (declared["type"], declared["default"]) == ("boolean", True)


@pytest.mark.parametrize(
    ("scope", "permissions", "needed"),
    [
        ("new-job", {"pull-requests": "write"}, {"contents": "read", "pull-requests": "write"}),
        ("prek", {"contents": "write"}, {"contents": "write"}),
        ("*", {"actions": "read"}, {"contents": "read", "actions": "read"}),
        ("prek", {"contents": "read", "issues": "none"}, {"contents": "read"}),
    ],
    ids=["a new job", "write over read", "top level", "none"],
)
def test_the_grant_check_sees_what_a_planted_permission_needs(
    workflows: dict[str, dict[str, Any]],
    scope: str,
    permissions: dict[str, str],
    needed: dict[str, str],
) -> None:
    planted = copy.deepcopy(workflows["ci.yml"])
    if scope == "*":
        planted["permissions"] = permissions
    else:
        planted["jobs"].setdefault(scope, {})["permissions"] = permissions
    assert needed_grants(planted) == needed


def test_only_build_uploads_dist_and_publish_uploads_what_it_downloads(
    workflows: dict[str, dict[str, Any]],
) -> None:
    artifacts = [
        (job_name, step["uses"].split("@")[0], step["with"])
        for job_name, job in workflows["release.yml"]["jobs"].items()
        for step in job.get("steps", [])
        if re.match(r"actions/(up|down)load-artifact@", step.get("uses", ""))
    ]
    assert [(job, action, inputs["name"]) for job, action, inputs in artifacts] == [
        ("build", "actions/upload-artifact", "dist"),
        ("scan", "actions/upload-artifact", "release-dependency-audit"),
        ("publish", "actions/download-artifact", "dist"),
    ]
    assert all("overwrite" not in inputs for _, _, inputs in artifacts)
    publish = workflows["release.yml"]["jobs"]["publish"]
    assert publish["environment"]["name"] == "pypi"
    assert publish["steps"][-1]["with"] == {"packages-dir": "dist/", "attestations": True}


def locked_uv() -> str:
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text())
    (version,) = [package["version"] for package in lock["package"] if package["name"] == "uv"]
    return str(version)


def test_the_release_s_uv_is_the_locked_uv_and_the_build_backend_allows_it(
    workflows: dict[str, dict[str, Any]],
) -> None:
    setups = {
        (step["with"]["version"], step["with"]["checksum"])
        for job in workflows["release.yml"]["jobs"].values()
        for step in job.get("steps", [])
        if step.get("uses", "").startswith("astral-sh/setup-uv@")
    }
    ((version, checksum),) = setups
    assert version == locked_uv()
    assert re.fullmatch(r"[0-9a-f]{64}", checksum)
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    (requirement,) = pyproject["build-system"]["requires"]
    match = re.fullmatch(r"uv_build>=([\d.]+),<([\d.]+)", requirement)
    assert match, requirement

    def parts(version: str) -> tuple[int, ...]:
        return tuple(int(part) for part in version.split("."))

    assert parts(match.group(1)) <= parts(version) < parts(match.group(2))


def test_the_audit_trees_and_trivy_are_one_set(workflows: dict[str, dict[str, Any]]) -> None:
    ci_env, release_env = workflows["ci.yml"]["env"], workflows["release.yml"]["env"]
    for name in ("AUDIT_DIR", "AUDIT_TREES", "TRIVY_URL", "TRIVY_SHA256"):
        assert release_env[name] == ci_env[name], name
    compiled = re.findall(
        r"^compile_tree (\S+)", (SCRIPTS / "audit-deps.sh").read_text(), flags=re.MULTILINE
    )
    assert ci_env["AUDIT_TREES"] == " ".join(compiled), "audit-report.sh reads one line"
    assert compiled == [
        "runtime-ceiling",
        "runtime-floor",
        "dev-ceiling",
        "docs-ceiling",
        "example-ceiling",
    ]


def test_the_example_is_its_own_project_whose_warnings_are_errors() -> None:
    # The example job runs pytest without -W error, which would override the example's
    # one ignore entry, so its filterwarnings turns every other warning into an error.
    # Its dependencies stay out of the root uv.lock; it has a lock of its own.
    example = REPO_ROOT / "examples" / "food-ordering-system"
    pyproject = (example / "pyproject.toml").read_text()
    assert re.search(r'filterwarnings = \[\n    "error",\n', pyproject)
    assert "[tool.uv.workspace]" not in (REPO_ROOT / "pyproject.toml").read_text()
    assert (example / "uv.lock").is_file()
