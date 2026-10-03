"""Tests that pin how .github/workflows/pages.yml deploys the API reference site.

The ref check runs against planted refs, tags and releases, with a stand-in for gh.

Run with:
uv run --only-dev pytest -c .github/scripts/pytest.ini .github/scripts/test_pages_workflow.py
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import pytest

from test_ci_checks import (
    DEFAULT_SHELL,
    REPO_ROOT,
    WORKFLOW,
    find_step,
    read_workflow,
    run_step,
    stand_in,
)

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path

PAGES = REPO_ROOT / ".github" / "workflows" / "pages.yml"
RELEASE = REPO_ROOT / ".github" / "workflows" / "release.yml"
REF_STEP = ("build", "Check that the ref is a published release")
RELEASE_TAG_STEP = ("tag", "Check the tag against the version")
PINNED = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")
TAG_PATTERN = re.compile(r"\[\[ ! \$TAG =~ (\S+) \]\]")
PUBLISHED = {"isDraft": False, "isPrerelease": False}


@pytest.fixture(scope="module")
def pages() -> dict[str, Any]:
    return read_workflow(PAGES)


def uses(job: dict[str, Any]) -> list[str]:
    return [step["uses"].split("@")[0] for step in job["steps"] if "uses" in step]


# --- what starts a deployment ------------------------------------------------------


def test_it_runs_when_release_yml_calls_it_or_by_hand_only(pages: dict[str, Any]) -> None:
    # No `release:` trigger: release.yml calls it after publish, so a release
    # that never reached PyPI gets no site.
    assert pages["on"] == {"workflow_call": None, "workflow_dispatch": None}


def test_no_job_is_skipped_by_an_if(pages: dict[str, Any]) -> None:
    # The ref check fails the run instead; an `if:` on deploy, such as always(),
    # could deploy without a build that passed it.
    for name, job in pages["jobs"].items():
        assert "if" not in job, name
    assert pages["jobs"]["deploy"]["needs"] == "build"
    assert "if" not in pages["jobs"]["build"]["steps"][0]


def test_one_deployment_runs_at_a_time_and_is_never_cancelled(pages: dict[str, Any]) -> None:
    assert pages["jobs"]["deploy"]["concurrency"] == {"group": "pages", "cancel-in-progress": False}
    assert "concurrency" not in pages


def test_only_the_deploy_job_can_write(pages: dict[str, Any]) -> None:
    assert pages["permissions"] == {}
    assert pages["jobs"]["build"]["permissions"] == {"contents": "read", "pages": "read"}
    assert pages["jobs"]["deploy"]["permissions"] == {"pages": "write", "id-token": "write"}
    assert pages["jobs"]["deploy"]["environment"]["name"] == "github-pages"
    assert "environment" not in pages["jobs"]["build"]


def test_the_build_checks_the_ref_then_builds_and_uploads_the_site(
    pages: dict[str, Any],
) -> None:
    build = pages["jobs"]["build"]
    assert build["steps"][0]["name"] == REF_STEP[1]
    assert uses(build) == [
        "actions/checkout",
        "actions/configure-pages",
        "astral-sh/setup-uv",
        "actions/upload-pages-artifact",
    ]
    runs = [step["name"] for step in build["steps"] if "run" in step]
    assert runs == [REF_STEP[1], "Build the docs site"]
    assert build["steps"][-2]["run"].strip() == ".github/scripts/build-docs.sh"
    assert build["steps"][-1]["with"] == {"path": "site/"}
    assert uses(pages["jobs"]["deploy"]) == ["actions/deploy-pages"]


def test_every_action_is_pinned_to_a_commit(pages: dict[str, Any]) -> None:
    for job in pages["jobs"].values():
        for step in job["steps"]:
            if "uses" in step:
                assert PINNED.match(step["uses"]), step["uses"]


def test_the_build_drops_credentials_and_uses_no_cache(pages: dict[str, Any]) -> None:
    for step in pages["jobs"]["build"]["steps"]:
        if step.get("uses", "").startswith("actions/checkout@"):
            assert step["with"]["persist-credentials"] is False
        assert not step.get("uses", "").startswith("actions/cache"), step
        assert step.get("with", {}).get("enable-cache", False) is False, step


def test_every_job_has_a_timeout_and_the_default_shell(pages: dict[str, Any]) -> None:
    assert pages["defaults"]["run"]["shell"] == DEFAULT_SHELL
    for name, job in pages["jobs"].items():
        assert job["timeout-minutes"] <= 10, name


def test_the_build_uses_the_ci_docs_job_s_action_pins(pages: dict[str, Any]) -> None:
    ci_steps = read_workflow(WORKFLOW)["jobs"]["docs"]["steps"]
    pins = {step["uses"].split("@")[0]: step for step in ci_steps if "uses" in step}
    for step in pages["jobs"]["build"]["steps"]:
        name = step.get("uses", "").split("@")[0]
        if name in pins:
            assert step["uses"] == pins[name]["uses"]
            assert step.get("with") == pins[name].get("with")


# --- the ref check -------------------------------------------------------------------


def check_ref(
    pages: dict[str, Any],
    tmp_path: Path,
    tag: str,
    *,
    ref_type: str = "tag",
    release: dict[str, bool] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the ref check on `tag`, with a gh that shows `release`, or finds none when None."""
    shown = tmp_path / "release.json"
    if release is None:
        gh = 'echo "$*" >"$RUNNER_TEMP/gh.txt"\necho "release not found" >&2\nexit 1'
    else:
        shown.write_text(json.dumps(release), encoding="utf-8")
        gh = f'echo "$*" >"$RUNNER_TEMP/gh.txt"\ncat "{shown}"'
    stand_in(tmp_path / "bin", "gh", gh)
    env = {"REF_TYPE": ref_type, "TAG": tag, "GH_TOKEN": "token", "GH_REPO": "permitio/permit-mcp"}
    return run_step(pages, REF_STEP, tmp_path, env)


def test_a_published_release_s_tag_passes(pages: dict[str, Any], tmp_path: Path) -> None:
    completed = check_ref(pages, tmp_path, "v1.2.3", release=PUBLISHED)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Deploying the site of the published release v1.2.3." in completed.stdout
    gh = (tmp_path / "gh.txt").read_text(encoding="utf-8").strip()
    assert gh == "release view v1.2.3 --json isPrerelease,isDraft"


@pytest.mark.parametrize(
    "tag", ["v1.2.3-rc1", "v1.2.3rc1", "vtest", "1.2.3", "v1.2", "v01.2.3", "v1.2.3\nv1.2.4"]
)
def test_a_tag_that_is_not_a_whole_version_tag_fails(
    pages: dict[str, Any], tmp_path: Path, tag: str
) -> None:
    completed = check_ref(pages, tmp_path, tag, release=PUBLISHED)
    assert completed.returncode == 1
    assert "is not a version tag such as v1.2.3" in completed.stdout
    assert not (tmp_path / "gh.txt").exists(), "gh is not asked about a tag that is no version"


@pytest.mark.parametrize("branch", ["main", "feature/docs"])
def test_a_branch_fails(pages: dict[str, Any], tmp_path: Path, branch: str) -> None:
    completed = check_ref(pages, tmp_path, branch, ref_type="branch", release=PUBLISHED)
    assert completed.returncode == 1
    assert "deploys from a release tag only; this run is on the branch" in completed.stdout


@pytest.mark.parametrize(
    "release",
    [
        {"isDraft": True, "isPrerelease": False},
        {"isDraft": False, "isPrerelease": True},
        {"isDraft": True, "isPrerelease": True},
    ],
    ids=["draft", "prerelease", "draft prerelease"],
)
def test_a_draft_or_a_pre_release_fails(
    pages: dict[str, Any], tmp_path: Path, release: dict[str, bool]
) -> None:
    completed = check_ref(pages, tmp_path, "v1.2.3", release=release)
    assert completed.returncode == 1
    assert "The release v1.2.3 is not a published full release" in completed.stdout


def test_a_tag_with_no_release_fails(pages: dict[str, Any], tmp_path: Path) -> None:
    completed = check_ref(pages, tmp_path, "v1.2.3", release=None)
    assert completed.returncode == 1
    assert "No published release has the tag v1.2.3." in completed.stdout


def test_the_tag_pattern_is_release_yml_s(pages: dict[str, Any]) -> None:
    release_check = find_step(read_workflow(RELEASE), RELEASE_TAG_STEP)["run"]
    ours = find_step(pages, REF_STEP)["run"]
    assert TAG_PATTERN.findall(ours) == TAG_PATTERN.findall(release_check)
    assert len(TAG_PATTERN.findall(ours)) == 1


def test_the_tag_reaches_the_shell_through_env_only(pages: dict[str, Any]) -> None:
    step = pages["jobs"]["build"]["steps"][0]
    assert "${{" not in step["run"]
    assert step["env"] == {
        "REF_TYPE": "${{ github.ref_type }}",
        "TAG": "${{ github.ref_name }}",
        "GH_TOKEN": "${{ github.token }}",
        "GH_REPO": "${{ github.repository }}",
    }
