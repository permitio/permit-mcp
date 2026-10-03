"""Tests for release-checks.sh, which holds a release and its site to a published version tag.

The tag check runs in planted checkouts with a stand-in uv, the check of the files to
upload on planted files, and the ref check of pages.yml with a stand-in gh.

Run with:
uv run --only-dev pytest -c .github/scripts/pytest.ini .github/scripts/test_release.py
"""

from __future__ import annotations

import json
import shutil
import tomllib
from typing import TYPE_CHECKING

import pytest

from harness import REPO_ROOT, git, run_script, stand_in, tool

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path

NOT_A_VERSION_TAG = [
    "1.2.3",
    "v1.2",
    "v1.2.3.4",
    "v01.2.3",
    "v1.2.3-rc.1",
    "v1.2.3rc1",
    "vtest",
    " v1.2.3",
    "v1.2.3 ",
    "v1.2.3\nv1.2.3",
    "v1.2.3\n",
    "V1.2.3",
    "",
]
PUBLISHED = {"isDraft": False, "isPrerelease": False}


# --- the tag check ----------------------------------------------------------------------


def checkout(tmp_path: Path, *, on_main: bool = True, main: bool = True) -> Path:
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


def check_tag(
    tmp_path: Path, repo: Path | None = None, uv: str = 'echo "1.2.3"', **env: str
) -> subprocess.CompletedProcess[str]:
    """Run the tag check of v1.2.3 on a release event of a full release, unless env says not."""
    stand_in(tmp_path, "uv", uv)
    env = {"EVENT": "release", "TAG": "v1.2.3", "PRERELEASE": "false", **env}
    return run_script(tmp_path, "release-checks.sh", "tag", env=env, cwd=repo or checkout(tmp_path))


def test_a_tag_of_the_version_on_main_passes(tmp_path: Path) -> None:
    completed = check_tag(tmp_path)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout == "Releasing 1.2.3 from v1.2.3, on main.\n"


def test_a_tag_on_main_s_history_passes(tmp_path: Path) -> None:
    repo = checkout(tmp_path)
    git(repo, "commit", "--quiet", "--allow-empty", "--message", "later")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "checkout", "--quiet", "HEAD~1")
    completed = check_tag(tmp_path, repo)
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize("tag", NOT_A_VERSION_TAG)
def test_a_tag_that_is_not_a_whole_version_tag_fails(tmp_path: Path, tag: str) -> None:
    completed = check_tag(tmp_path, TAG=tag)
    assert completed.returncode == 1
    assert "is not a version tag such as v1.2.3." in completed.stdout
    assert completed.stdout.count("\n") == 1, "the tag is printed quoted, on one line"


UNREADABLE_VERSION = "Could not read the version from pyproject.toml."


@pytest.mark.parametrize(
    ("env", "repo", "status", "says"),
    [
        ({"PRERELEASE": "true"}, {}, 1, "The release is marked as a pre-release"),
        ({"PRERELEASE": ""}, {}, 1, "The release is marked as a pre-release"),
        ({"TAG": "v1.2.4"}, {}, 1, "The tag is v1.2.4, but pyproject.toml's version is 1.2.3."),
        ({}, {"on_main": False}, 1, "v1.2.3 is not on main. Tag a commit of main."),
        ({}, {"main": False}, 2, "::error title=Release tag::Could not read origin/main."),
        ({"uv": "exit 1"}, {}, 2, UNREADABLE_VERSION),
        ({"uv": "true"}, {}, 2, UNREADABLE_VERSION),
        ({"EVENT": "workflow_dispatch", "TAG": ""}, {"on_main": False, "main": False}, 0,
         "::notice title=Dry run::Building and scanning 1.2.3; nothing is published."),
    ],
    ids=[
        "a pre-release", "no pre-release flag", "another version", "off main", "no main",
        "uv fails", "uv prints nothing", "a dry run",
    ],
)  # fmt: skip
def test_the_tag_check_fails_a_release_it_cannot_prove_and_passes_a_dry_run(
    tmp_path: Path, env: dict[str, str], repo: dict[str, bool], status: int, says: str
) -> None:
    completed = check_tag(tmp_path, checkout(tmp_path, **repo), **env)
    assert completed.returncode == status, completed.stdout + completed.stderr
    assert says in completed.stdout


def test_the_tag_check_reads_this_repository_s_version(tmp_path: Path) -> None:
    version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"]
    completed = check_tag(tmp_path, uv=f'exec "{tool("uv")}" "$@"', TAG=f"v{version}")
    assert completed.returncode == 0, completed.stdout + completed.stderr


# --- the files publish uploads ------------------------------------------------------------

WHEEL = "permit_mcp-1.2.3-py3-none-any.whl"
SDIST = "permit_mcp-1.2.3.tar.gz"


@pytest.mark.parametrize(
    ("names", "tag", "status"),
    [
        ([SDIST, WHEEL], "v1.2.3", 0),
        ([WHEEL], "v1.2.3", 1),
        ([SDIST], "v1.2.3", 1),
        ([], "v1.2.3", 1),
        ([WHEEL, SDIST, "permit_mcp-1.2.2-py3-none-any.whl"], "v1.2.3", 1),
        ([WHEEL, SDIST, ".hidden"], "v1.2.3", 1),
        (["permit_mcp-1.2.4-py3-none-any.whl", "permit_mcp-1.2.4.tar.gz"], "v1.2.3", 1),
        ([WHEEL.replace("py3-none-any", "cp311-cp311-linux_x86_64"), SDIST], "v1.2.3", 1),
        ([WHEEL, SDIST], "v1.2.4", 1),
    ],
    ids=[
        "the tag's wheel and sdist", "no sdist", "no wheel", "empty", "another wheel",
        "a hidden file", "another version", "another wheel tag", "another tag",
    ],
)  # fmt: skip
def test_publish_uploads_exactly_the_tag_s_wheel_and_sdist(
    tmp_path: Path, names: list[str], tag: str, status: int
) -> None:
    (tmp_path / "dist").mkdir()
    for name in names:
        (tmp_path / "dist" / name).write_text("")
    dist = tmp_path / "dist"
    completed = run_script(tmp_path, "release-checks.sh", "files", dist, env={"TAG": tag})
    assert completed.returncode == status, completed.stdout + completed.stderr
    version = tag.removeprefix("v")
    if status == 0:
        assert completed.stdout == f"Uploading {WHEEL} and {SDIST}.\n"
    else:
        assert "::error title=Publish::" in completed.stdout
        expected = f"permit_mcp-{version}-py3-none-any.whl, permit_mcp-{version}.tar.gz."
        assert completed.stdout.rstrip().endswith(f"expected {expected}")


# --- the ref check of pages.yml -------------------------------------------------------------


def check_ref(
    tmp_path: Path, tag: str, ref_type: str = "tag", release: dict[str, bool] | None = PUBLISHED
) -> subprocess.CompletedProcess[str]:
    """Run the ref check on `tag`, with a gh that shows `release`, or finds none when None."""
    shown = "release not found >&2; exit 1" if release is None else f"'{json.dumps(release)}'"
    stand_in(tmp_path, "gh", f'echo "$*" >"$RUNNER_TEMP/gh.txt"\necho {shown}')
    env = {"REF_TYPE": ref_type, "TAG": tag, "GH_TOKEN": "token", "GH_REPO": "o/r"}
    return run_script(tmp_path, "release-checks.sh", "ref", env=env)


def test_a_published_release_s_tag_passes(tmp_path: Path) -> None:
    completed = check_ref(tmp_path, "v1.2.3")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Deploying the site of the published release v1.2.3." in completed.stdout
    gh = (tmp_path / "gh.txt").read_text().strip()
    assert gh == "release view v1.2.3 --json isPrerelease,isDraft"


ON_A_BRANCH = "deploys from a release tag only; this run is on the branch"
NOT_FULL = "is not a published full release"


@pytest.mark.parametrize(
    ("tag", "ref_type", "release", "says"),
    [
        *[(tag, "tag", PUBLISHED, "is not a version tag such") for tag in NOT_A_VERSION_TAG],
        ("main", "branch", PUBLISHED, ON_A_BRANCH),
        ("v1.2.3", "branch", PUBLISHED, ON_A_BRANCH),
        ("v1.2.3", "tag", None, "No published release has the tag v1.2.3."),
        ("v1.2.3", "tag", {"isDraft": True, "isPrerelease": False}, NOT_FULL),
        ("v1.2.3", "tag", {"isDraft": False, "isPrerelease": True}, NOT_FULL),
        ("v1.2.3", "tag", {"isDraft": True, "isPrerelease": True}, NOT_FULL),
    ],
)  # fmt: skip
def test_the_site_deploys_from_a_published_full_release_s_version_tag_only(
    tmp_path: Path, tag: str, ref_type: str, release: dict[str, bool] | None, says: str
) -> None:
    completed = check_ref(tmp_path, tag, ref_type, release)
    assert completed.returncode == 1
    assert says in completed.stdout
    asked = (tmp_path / "gh.txt").exists()
    assert asked == (ref_type == "tag" and tag == "v1.2.3"), "gh is asked about versions only"
