"""Tests for fetch-binary.sh, against planted tarballs served from file:// URLs.

Run with:
uv run --only-dev pytest -c .github/scripts/pytest.ini .github/scripts/test_fetch_binary.py
"""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent / "fetch-binary.sh"


def fetch(*args: str) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    assert bash is not None
    return subprocess.run([bash, str(SCRIPT), *args], capture_output=True, text=True, check=False)


def tarball(tmp_path: Path, members: dict[str, int]) -> tuple[str, str]:
    """A tar.gz holding each member with the given mode; returns its URL and SHA-256."""
    path = tmp_path / "release.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, mode in members.items():
            content = b"#!/bin/sh\necho planted\n"
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = mode
            archive.addfile(info, io.BytesIO(content))
    return path.as_uri(), hashlib.sha256(path.read_bytes()).hexdigest()


def test_installs_the_named_executable(tmp_path: Path) -> None:
    url, sha256 = tarball(tmp_path, {"tool": 0o755, "README.md": 0o644})
    dest = tmp_path / "bin"
    result = fetch(url, sha256, "tool", str(dest))
    assert result.returncode == 0, result.stdout + result.stderr
    assert (dest / "tool").stat().st_mode & 0o111
    assert not (dest / "README.md").exists()
    ran = subprocess.run([dest / "tool"], capture_output=True, text=True, check=True)
    assert ran.stdout == "planted\n"


def test_a_planted_checksum_mismatch_installs_nothing(tmp_path: Path) -> None:
    url, sha256 = tarball(tmp_path, {"tool": 0o755})
    dest = tmp_path / "bin"
    result = fetch(
        url, sha256.replace(sha256[0], "0" if sha256[0] != "0" else "1"), "tool", str(dest)
    )
    assert result.returncode == 1
    assert "not the pinned" in result.stdout
    assert not (dest / "tool").exists()


@pytest.mark.parametrize("members", [{"other": 0o755}, {"tool": 0o644}], ids=["absent", "not +x"])
def test_a_tarball_without_the_executable_fails(tmp_path: Path, members: dict[str, int]) -> None:
    url, sha256 = tarball(tmp_path, members)
    dest = tmp_path / "bin"
    result = fetch(url, sha256, "tool", str(dest))
    assert result.returncode == 1
    assert "holds no executable tool" in result.stdout
    assert not (dest / "tool").exists()


def test_a_failed_download_fails(tmp_path: Path) -> None:
    result = fetch((tmp_path / "absent.tar.gz").as_uri(), "0" * 64, "tool", str(tmp_path / "b"))
    assert result.returncode == 1
    assert "Could not download" in result.stdout


@pytest.mark.parametrize("count", [0, 3, 5])
def test_a_wrong_number_of_arguments_exits_2(count: int) -> None:
    assert fetch(*["x"] * count).returncode == 2
