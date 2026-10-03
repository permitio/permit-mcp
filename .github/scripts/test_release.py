"""Tests for .github/workflows/release.yml.

They pin how the release is gated: what triggers it, which job needs which,
what each job may do, which files reach PyPI, that it calls ci.yml with every
permission ci.yml's jobs ask for, and that it pins the tools ci.yml and uv.lock
pin. The tag check, the scan gate and the check of the files to upload run
against planted tags, repositories, Trivy reports and files, with the helpers of
test_ci_checks.py. zizmor (the prek hook and CI) covers caches, checkout
credentials and default permissions, so they are not tested here.

Run with:
uv run --only-dev pytest -c .github/scripts/pytest.ini .github/scripts/test_release.py
"""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from typing import TYPE_CHECKING, Any

import pytest

from test_ci_checks import (
    GIT_ENV,
    REPO_ROOT,
    WORKFLOW,
    find_step,
    git,
    read_workflow,
    run_step,
    stand_in,
)

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path

RELEASE = REPO_ROOT / ".github" / "workflows" / "release.yml"
TAG_STEP = ("tag", "Check the tag against the version")
GATE_STEP = ("scan", "Gate on fixable HIGH and CRITICAL advisories in the runtime trees")
FILES_STEP = ("publish", "Check the files to upload")
JOB_NEEDS = {
    "tag": None,
    "ci": "tag",
    "build": "ci",
    "scan": "build",
    "publish": "scan",
    "docs": "publish",
}
PAGES = REPO_ROOT / ".github" / "workflows" / "pages.yml"
# The order of the levels a permission can be granted at.
LEVELS = ("none", "read", "write")
# What a job may be granted for a permission a called job asks for.
COVERS = {"none": {"none", "read", "write"}, "read": {"read", "write"}, "write": {"write"}}


@pytest.fixture(scope="module")
def release() -> dict[str, Any]:
    return read_workflow(RELEASE)


@pytest.fixture(scope="module")
def ci() -> dict[str, Any]:
    return read_workflow(WORKFLOW)


@pytest.fixture(scope="module")
def pages() -> dict[str, Any]:
    return read_workflow(PAGES)


def steps(release: dict[str, Any], uses: str) -> list[dict[str, Any]]:
    """Every step of release.yml that runs the action `uses`, in job order."""
    return [
        step
        for job in release["jobs"].values()
        for step in job.get("steps", [])
        if step.get("uses", "").startswith(f"{uses}@")
    ]


# --- what starts a release, and what it may do ------------------------------------


def test_a_published_release_or_a_dry_run_starts_it(release: dict[str, Any]) -> None:
    # `created` and `released` would also fire for drafts or skip prereleases.
    assert release["on"] == {"release": {"types": ["published"]}, "workflow_dispatch": None}


def test_steps_run_with_errexit_nounset_and_pipefail(
    release: dict[str, Any], ci: dict[str, Any]
) -> None:
    assert release["defaults"] == ci["defaults"]


def test_each_job_needs_the_one_before_it(release: dict[str, Any]) -> None:
    assert {name: job.get("needs") for name, job in release["jobs"].items()} == JOB_NEEDS


def test_one_run_per_ref_at_a_time_and_none_cancelled(release: dict[str, Any]) -> None:
    assert release["concurrency"] == {
        "group": "${{ github.workflow }}-${{ github.ref }}",
        "cancel-in-progress": False,
    }


def test_only_a_release_event_publishes(release: dict[str, Any]) -> None:
    gated = {name: job["if"] for name, job in release["jobs"].items() if "if" in job}
    assert gated == {"publish": "github.event_name == 'release'"}


def test_only_publish_holds_an_oidc_token_or_an_environment(release: dict[str, Any]) -> None:
    for name, job in release["jobs"].items():
        if name == "publish":
            assert job["permissions"] == {"id-token": "write"}
            assert job["environment"]["name"] == "pypi"
        elif name == "docs":
            assert "environment" not in job, "pages.yml's deploy job holds github-pages"
        else:
            assert job["permissions"] == {"contents": "read"}, name
            assert "environment" not in job, name


def test_publish_uploads_only_the_checked_dist_with_attestations(release: dict[str, Any]) -> None:
    publish = release["jobs"]["publish"]["steps"]
    assert [step.get("uses", step.get("name", "")).split("@")[0] for step in publish] == [
        "actions/download-artifact",
        FILES_STEP[1],
        "pypa/gh-action-pypi-publish",
    ]
    download, _, upload = publish
    assert download["with"] == {"name": "dist", "path": "dist/"}
    assert upload["with"] == {"packages-dir": "dist/", "attestations": True}
    assert "password" not in upload["with"], "trusted publishing needs no token"


def test_only_build_uploads_dist_and_nothing_is_overwritten(release: dict[str, Any]) -> None:
    uploads = [
        (name, step["with"])
        for name, job in release["jobs"].items()
        for step in job.get("steps", [])
        if step.get("uses", "").startswith("actions/upload-artifact@")
    ]
    named_dist = [(name, upload) for name, upload in uploads if upload["name"] == "dist"]
    assert named_dist == [
        (
            "build",
            {"name": "dist", "path": "dist/", "retention-days": 7, "if-no-files-found": "error"},
        )
    ]
    for name, upload in uploads:
        assert "overwrite" not in upload, name


def test_build_builds_once_and_checks_what_it_built(release: dict[str, Any]) -> None:
    build = release["jobs"]["build"]
    assert find_step(release, ("build", "Build"))["run"] == "uv build --no-sources --out-dir dist"
    check = " ".join(find_step(release, ("build", "Check the wheel and sdist"))["run"].split())
    assert check == (
        "uv run --no-project --python 3.11 python .github/scripts/check_dist.py dist pyproject.toml"
    )
    assert build["steps"][-1]["uses"].startswith("actions/upload-artifact@")
    assert sum("uv build" in step.get("run", "") for step in build["steps"]) == 1
    others = [name for name, job in release["jobs"].items() if name != "build"]
    for name in others:
        runs = " ".join(step.get("run", "") for step in release["jobs"][name].get("steps", []))
        assert "uv build" not in runs, f"{name} builds again"


def test_no_step_may_fail(release: dict[str, Any]) -> None:
    for name, job in release["jobs"].items():
        assert "continue-on-error" not in job, name
        for step in job.get("steps", []):
            assert "continue-on-error" not in step, step


def test_every_job_has_a_timeout(release: dict[str, Any]) -> None:
    for name, job in release["jobs"].items():
        if "uses" not in job:
            assert job["timeout-minutes"] <= 15, name


def test_every_job_checks_out_the_tagged_commit(release: dict[str, Any]) -> None:
    checkouts = {
        name: step["with"]
        for name, job in release["jobs"].items()
        for step in job.get("steps", [])
        if step.get("uses", "").startswith("actions/checkout@")
    }
    assert checkouts == {
        "tag": {"fetch-depth": 0, "persist-credentials": False},
        "build": {"persist-credentials": False},
        "scan": {"persist-credentials": False},
    }


# --- the CI call ------------------------------------------------------------------


def asked_for(called: dict[str, Any]) -> set[tuple[str, str]]:
    """Every (permission, level) the called workflow or one of its jobs asks for."""
    asked: set[tuple[str, str]] = set()
    for scope in [called, *called["jobs"].values()]:
        asked |= set((scope.get("permissions") or {}).items())
    return asked


def missing_grants(called: dict[str, Any], release: dict[str, Any], job: str = "ci") -> list[str]:
    """The permissions a called workflow's jobs ask for that release.yml's `job` does not grant.

    GitHub refuses the whole release run when any of them is missing, even for a
    job the run would skip.
    """
    asked = asked_for(called)
    granted = release["jobs"][job].get("permissions") or {}
    return sorted(
        f"{name}: {level}"
        for name, level in asked
        if granted.get(name, "none") not in COVERS[level]
    )


def test_ci_runs_the_whole_ci_workflow(release: dict[str, Any], ci: dict[str, Any]) -> None:
    job = release["jobs"]["ci"]
    assert job["uses"] == "./.github/workflows/ci.yml"
    assert "secrets" not in job, "no secret is passed, and never `secrets: inherit`"
    assert "workflow_call" in ci["on"]


def test_ci_does_not_gate_the_release_on_the_dev_tree(
    release: dict[str, Any], ci: dict[str, Any]
) -> None:
    assert release["jobs"]["ci"]["with"] == {"gate-dev-tree": False}
    declared = ci["on"]["workflow_call"]["inputs"]
    assert declared["gate-dev-tree"]["type"] == "boolean"


def test_the_release_grants_every_permission_ci_asks_for(
    release: dict[str, Any], ci: dict[str, Any]
) -> None:
    assert missing_grants(ci, release) == []


def test_a_permission_a_new_ci_job_asks_for_is_missing(
    release: dict[str, Any], ci: dict[str, Any]
) -> None:
    planted = json.loads(json.dumps(ci))
    planted["jobs"]["comment"] = {"permissions": {"pull-requests": "write", "contents": "read"}}
    assert missing_grants(planted, release) == ["pull-requests: write"]


def test_a_read_grant_does_not_cover_a_write(release: dict[str, Any], ci: dict[str, Any]) -> None:
    planted = json.loads(json.dumps(ci))
    planted["jobs"]["prek"]["permissions"] = {"contents": "write"}
    assert missing_grants(planted, release) == ["contents: write"]


def test_a_grant_removed_from_the_release_is_missing(
    release: dict[str, Any], ci: dict[str, Any]
) -> None:
    planted = json.loads(json.dumps(release))
    del planted["jobs"]["ci"]["permissions"]
    assert missing_grants(ci, planted) == ["contents: read"]


def test_ci_s_top_level_permissions_count(release: dict[str, Any], ci: dict[str, Any]) -> None:
    planted = json.loads(json.dumps(ci))
    planted["permissions"] = {"actions": "read"}
    assert missing_grants(planted, release) == ["actions: read"]


def test_a_none_level_needs_no_grant(release: dict[str, Any], ci: dict[str, Any]) -> None:
    planted = json.loads(json.dumps(ci))
    planted["jobs"]["prek"]["permissions"] = {"contents": "read", "issues": "none"}
    assert missing_grants(planted, release) == []


# --- pinned tools -------------------------------------------------------------------


def locked_uv() -> str:
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text())
    versions = [package["version"] for package in lock["package"] if package["name"] == "uv"]
    assert len(versions) == 1
    version: str = versions[0]
    return version


def test_every_uv_is_the_locked_uv_with_one_checksum(release: dict[str, Any]) -> None:
    setups = steps(release, "astral-sh/setup-uv")
    assert len(setups) == 3
    assert {step["with"]["version"] for step in setups} == {locked_uv()}
    checksums = {step["with"]["checksum"] for step in setups}
    assert len(checksums) == 1
    assert re.fullmatch(r"[0-9a-f]{64}", checksums.pop())
    for step in setups:
        assert "version-file" not in step["with"]


def test_the_build_backend_allows_the_release_uv(release: dict[str, Any]) -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    (requirement,) = pyproject["build-system"]["requires"]
    match = re.fullmatch(r"uv_build>=([\d.]+),<([\d.]+)", requirement)
    assert match, requirement

    def parts(version: str) -> tuple[int, ...]:
        return tuple(int(part) for part in version.split("."))

    release_uv = parts(steps(release, "astral-sh/setup-uv")[0]["with"]["version"])
    assert parts(match.group(1)) <= release_uv < parts(match.group(2))


def test_trivy_and_the_audit_dir_are_ci_s(release: dict[str, Any], ci: dict[str, Any]) -> None:
    for name in ("AUDIT_DIR", "TRIVY_URL", "TRIVY_SHA256"):
        assert release["env"][name] == ci["env"][name], name


# --- the tag check --------------------------------------------------------------------


def planted_checkout(tmp_path: Path, *, on_main: bool = True, main: bool = True) -> Path:
    """A tag job's checkout: HEAD is the tagged commit, origin/main a commit of main.

    With `on_main` false, HEAD is one commit past main; with `main` false, there is
    no origin/main.
    """
    repo = tmp_path / "checkout"
    repo.mkdir()
    git(repo, "init", "--quiet", "--initial-branch", "main")
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy(REPO_ROOT / name, repo / name)
    git(repo, "add", "pyproject.toml", "uv.lock")
    git(repo, "commit", "--quiet", "--message", "main")
    if main:
        git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    if not on_main:
        git(repo, "commit", "--quiet", "--allow-empty", "--message", "unmerged")
    return repo


def run_tag_check(
    release: dict[str, Any],
    tmp_path: Path,
    tag: str,
    *,
    checkout: Path | None = None,
    uv: str = 'echo "1.2.3"',
    **env: str,
) -> subprocess.CompletedProcess[str]:
    """Run the tag check on a release event of a full release, unless env says otherwise."""
    stand_in(tmp_path / "bin", "uv", uv)
    checkout = checkout or planted_checkout(tmp_path)
    env = {**GIT_ENV, "EVENT": "release", "TAG": tag, "PRERELEASE": "false", **env}
    return run_step(release, TAG_STEP, tmp_path, env, cwd=checkout)


def test_a_tag_of_the_version_on_main_passes(release: dict[str, Any], tmp_path: Path) -> None:
    result = run_tag_check(release, tmp_path, "v1.2.3")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Releasing 1.2.3 from v1.2.3, on main." in result.stdout


def test_a_tag_on_main_s_history_passes(release: dict[str, Any], tmp_path: Path) -> None:
    checkout = planted_checkout(tmp_path)
    git(checkout, "commit", "--quiet", "--allow-empty", "--message", "later")
    git(checkout, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(checkout, "checkout", "--quiet", "HEAD~1")
    result = run_tag_check(release, tmp_path, "v1.2.3", checkout=checkout)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "tag",
    [
        "1.2.3",
        "v1.2",
        "v1.2.3.4",
        "v01.2.3",
        "v1.2.3-rc.1",
        "v1.2.3rc1",
        " v1.2.3",
        "v1.2.3 ",
        "v1.2.3\nv1.2.3",
        "v1.2.3\n",
        "V1.2.3",
        "",
    ],
)
def test_a_tag_that_is_not_a_whole_version_tag_fails(
    release: dict[str, Any], tmp_path: Path, tag: str
) -> None:
    result = run_tag_check(release, tmp_path, tag)
    assert result.returncode == 1
    assert "is not a version tag such as v1.2.3." in result.stdout
    assert result.stdout.count("\n") == 1, "the tag is printed quoted, on one line"


@pytest.mark.parametrize("prerelease", ["true", ""])
def test_a_pre_release_fails(release: dict[str, Any], tmp_path: Path, prerelease: str) -> None:
    result = run_tag_check(release, tmp_path, "v1.2.3", PRERELEASE=prerelease)
    assert result.returncode == 1
    assert "The release is marked as a pre-release" in result.stdout


def test_a_tag_of_another_version_fails(release: dict[str, Any], tmp_path: Path) -> None:
    result = run_tag_check(release, tmp_path, "v1.2.4")
    assert result.returncode == 1
    assert "The tag is v1.2.4, but pyproject.toml's version is 1.2.3." in result.stdout


def test_a_tag_off_main_fails(release: dict[str, Any], tmp_path: Path) -> None:
    checkout = planted_checkout(tmp_path, on_main=False)
    result = run_tag_check(release, tmp_path, "v1.2.3", checkout=checkout)
    assert result.returncode == 1
    assert "v1.2.3 is not on main. Tag a commit of main." in result.stdout


def test_a_checkout_without_main_exits_2(release: dict[str, Any], tmp_path: Path) -> None:
    checkout = planted_checkout(tmp_path, main=False)
    result = run_tag_check(release, tmp_path, "v1.2.3", checkout=checkout)
    assert result.returncode == 2
    assert "Could not read origin/main." in result.stdout


def test_a_dry_run_reports_the_version_and_checks_no_tag(
    release: dict[str, Any], tmp_path: Path
) -> None:
    checkout = planted_checkout(tmp_path, on_main=False, main=False)
    result = run_tag_check(release, tmp_path, "", EVENT="workflow_dispatch", checkout=checkout)
    assert result.returncode == 0
    assert "::notice title=Dry run::Building and scanning 1.2.3; nothing is published." in (
        result.stdout
    )


@pytest.mark.parametrize("uv", ["exit 1", "true"], ids=["uv fails", "uv prints nothing"])
def test_an_unreadable_version_exits_2(release: dict[str, Any], tmp_path: Path, uv: str) -> None:
    result = run_tag_check(release, tmp_path, "v1.2.3", uv=uv)
    assert result.returncode == 2
    assert "Could not read the version from pyproject.toml." in result.stdout


def test_the_tag_check_reads_this_repository_s_version(
    release: dict[str, Any], tmp_path: Path
) -> None:
    version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"]
    uv = shutil.which("uv")
    assert uv is not None, "uv is not on PATH; uv run puts it there"
    result = run_tag_check(release, tmp_path, f"v{version}", uv=f'exec "{uv}" "$@"')
    assert result.returncode == 0, result.stdout + result.stderr


# --- the files publish uploads ------------------------------------------------------------


def check_files(
    release: dict[str, Any], tmp_path: Path, names: list[str], tag: str = "v1.2.3"
) -> subprocess.CompletedProcess[str]:
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in names:
        (dist / name).write_text("")
    return run_step(release, FILES_STEP, tmp_path, {"TAG": tag}, cwd=tmp_path)


WHEEL = "permit_mcp-1.2.3-py3-none-any.whl"
SDIST = "permit_mcp-1.2.3.tar.gz"


def test_the_tag_s_wheel_and_sdist_pass(release: dict[str, Any], tmp_path: Path) -> None:
    result = check_files(release, tmp_path, [SDIST, WHEEL])
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Uploading {WHEEL} and {SDIST}." in result.stdout


@pytest.mark.parametrize(
    "names",
    [
        [WHEEL],
        [SDIST],
        [],
        [WHEEL, SDIST, "permit_mcp-1.2.2-py3-none-any.whl"],
        [WHEEL, SDIST, ".hidden"],
        ["permit_mcp-1.2.4-py3-none-any.whl", "permit_mcp-1.2.4.tar.gz"],
        [WHEEL.replace("py3-none-any", "cp311-cp311-linux_x86_64"), SDIST],
    ],
    ids=[
        "no sdist",
        "no wheel",
        "empty",
        "another wheel",
        "a hidden file",
        "other version",
        "other wheel tag",
    ],
)
def test_other_files_fail(release: dict[str, Any], tmp_path: Path, names: list[str]) -> None:
    result = check_files(release, tmp_path, names)
    assert result.returncode == 1
    assert "::error title=Publish::dist/ holds" in result.stdout


def test_files_of_another_tag_fail(release: dict[str, Any], tmp_path: Path) -> None:
    result = check_files(release, tmp_path, [WHEEL, SDIST], tag="v1.2.4")
    assert result.returncode == 1
    assert "expected permit_mcp-1.2.4-py3-none-any.whl, permit_mcp-1.2.4.tar.gz." in result.stdout


# --- the scan gate ----------------------------------------------------------------------


def plant_tree(directory: Path, tree: str, *, vulnerable: bool) -> None:
    package = {"Name": "p", "Version": "1"}
    result: dict[str, Any] = {"Target": "requirements.txt", "Packages": [package]}
    if vulnerable:
        advisory = {"VulnerabilityID": "CVE-1", "PkgName": "p", "Severity": "HIGH"}
        result["Vulnerabilities"] = [{**advisory, "FixedVersion": "2"}]
    (directory / tree).mkdir(parents=True)
    (directory / tree / "requirements.txt").write_text("p==1\n")
    (directory / f"trivy-{tree}.json").write_text(json.dumps({"Results": [result]}))


def run_gate(
    release: dict[str, Any], tmp_path: Path, vulnerable: set[str], absent: str = ""
) -> subprocess.CompletedProcess[str]:
    audit = tmp_path / "audit"
    for tree in ("runtime-ceiling", "runtime-floor", "dev-ceiling"):
        if tree != absent:
            plant_tree(audit, tree, vulnerable=tree in vulnerable)
    return run_step(release, GATE_STEP, tmp_path, {"AUDIT_DIR": str(audit)})


def test_the_gate_passes_clean_runtime_trees(release: dict[str, Any], tmp_path: Path) -> None:
    result = run_gate(release, tmp_path, vulnerable={"dev-ceiling"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "No fixable HIGH or CRITICAL advisory in the runtime trees." in result.stdout


@pytest.mark.parametrize("tree", ["runtime-ceiling", "runtime-floor"])
def test_the_gate_fails_an_advisory_in_a_runtime_tree(
    release: dict[str, Any], tmp_path: Path, tree: str
) -> None:
    result = run_gate(release, tmp_path, vulnerable={tree})
    assert result.returncode == 1
    assert "::error title=HIGH: CVE-1 in p::" in result.stdout, "annotated"
    assert "::error title=Release scan::Fixable HIGH or CRITICAL advisories" in result.stdout


def test_the_gate_exits_2_when_a_runtime_scan_is_missing(
    release: dict[str, Any], tmp_path: Path
) -> None:
    result = run_gate(release, tmp_path, vulnerable=set(), absent="runtime-floor")
    assert result.returncode == 2
    assert "::error title=Release scan::The scan did not complete" in result.stdout


def test_the_scan_runs_ci_s_audit_script(release: dict[str, Any]) -> None:
    scan = find_step(release, ("scan", "Scan the dependency trees"))
    assert scan["run"] == '.github/scripts/audit-deps.sh "$AUDIT_DIR"'
    assert "if" not in scan
    assert "if" not in find_step(release, GATE_STEP)


# --- the docs call ----------------------------------------------------------------


def needed_grants(called: dict[str, Any]) -> dict[str, str]:
    """The least grant that covers every permission the called workflow's jobs ask for."""
    needed: dict[str, str] = {}
    for name, level in asked_for(called):
        if level != "none" and LEVELS.index(level) > LEVELS.index(needed.get(name, "none")):
            needed[name] = level
    return needed


def test_docs_publishes_the_site_once_the_release_is_on_pypi(
    release: dict[str, Any], pages: dict[str, Any]
) -> None:
    job = release["jobs"]["docs"]
    assert job["needs"] == "publish"
    assert job["uses"] == "./.github/workflows/pages.yml"
    assert "secrets" not in job
    assert "with" not in job
    assert "if" not in job, "publish's if already skips it on a dry run"
    assert "workflow_call" in pages["on"]


def test_docs_grants_exactly_what_pages_asks_for(
    release: dict[str, Any], pages: dict[str, Any]
) -> None:
    assert missing_grants(pages, release, "docs") == []
    assert release["jobs"]["docs"]["permissions"] == needed_grants(pages)
    assert needed_grants(pages) == {"contents": "read", "pages": "write", "id-token": "write"}


def test_a_permission_a_new_pages_job_asks_for_is_missing(
    release: dict[str, Any], pages: dict[str, Any]
) -> None:
    planted = json.loads(json.dumps(pages))
    planted["jobs"]["deploy"]["permissions"]["contents"] = "write"
    assert missing_grants(planted, release, "docs") == ["contents: write"]
    assert needed_grants(planted)["contents"] == "write"
