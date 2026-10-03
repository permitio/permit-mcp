"""What the tests of the CI scripts share: running a script with stand-ins, and planted git.

A stand-in is an executable that a test puts first on PATH in place of a tool the
script calls, such as uv or gitleaks.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS.parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "planted",
    "GIT_AUTHOR_EMAIL": "planted@example.invalid",
    "GIT_COMMITTER_NAME": "planted",
    "GIT_COMMITTER_EMAIL": "planted@example.invalid",
}


def tool(name: str) -> str:
    """Return the path of `name` on PATH, failing the test when it is not there."""
    path = shutil.which(name)
    if path is None:
        pytest.fail(f"{name} is not on PATH; these tests run it")
    return path


def read_workflow(name: str) -> dict[str, Any]:
    """Read .github/workflows/`name` with yq, as the needs check reads it."""
    completed = subprocess.run(
        [tool("yq"), "-o=json", ".", str(WORKFLOWS / name)],
        capture_output=True,
        text=True,
        check=True,
    )
    workflow: dict[str, Any] = json.loads(completed.stdout)
    return workflow


def stand_in(tmp_path: Path, name: str, script: str) -> None:
    """Write `script` as an executable bash script named `name` in tmp_path/bin."""
    (tmp_path / "bin").mkdir(exist_ok=True)
    path = tmp_path / "bin" / name
    path.write_text(f"#!/usr/bin/env bash\n{script}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def run_script(
    tmp_path: Path, *argv: str | Path, env: dict[str, str] | None = None, cwd: Path = REPO_ROOT
) -> subprocess.CompletedProcess[str]:
    """Run .github/scripts/argv[0] as a step runs it, with argv[1:] as its arguments.

    tmp_path is RUNNER_TEMP, and tmp_path/bin goes first on PATH, for stand-ins. The
    script runs from cwd, the repository root unless a test plants a checkout, with
    the git identity of GIT_ENV and `env` on top.
    """
    script, *args = argv
    return subprocess.run(
        [tool("bash"), str(SCRIPTS / script), *map(str, args)],
        env={
            **GIT_ENV,
            "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "HOME": os.environ["HOME"],
            "RUNNER_TEMP": str(tmp_path),
            **(env or {}),
        },
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def git(repo: Path, *args: str) -> str:
    """Run git in `repo` with the planted identity; return its output, stripped."""
    completed = subprocess.run(
        [tool("git"), "-C", str(repo), *args],
        env={**GIT_ENV, "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()
