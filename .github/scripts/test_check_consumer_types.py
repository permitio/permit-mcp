"""Tests for check-consumer-types.sh, with planted wheels and fixtures and the real mypy and uv.

Each planted wheel holds a small `permit_mcp` package, so the tests need no network and do not
depend on the real package's API.

Run with:
uv run --only-dev pytest -c .github/scripts/pytest.ini .github/scripts/test_check_consumer_types.py
"""

from __future__ import annotations

import base64
import hashlib
import re
import tomllib
import zipfile
from typing import TYPE_CHECKING

import pytest

from harness import REPO_ROOT, SCRIPTS, run_script, tool

if TYPE_CHECKING:
    from pathlib import Path

SCRIPT = "check-consumer-types.sh"
DID_NOT_RUN = "::error title=Consumer types::did not run:"
FAILED = "::error title=Consumer types::fixture.py does not type-check against the wheel."

API = """\
from typing_extensions import deprecated


def register(*, exclude: list[str]) -> list[str]:
    return exclude


@deprecated("use register")
def add(names: list[str]) -> None: ...
"""
# The misuse type-checks once `exclude` takes any object.
LOOSER_API = API.replace("exclude: list[str]", "exclude: object")

FIXTURE = """\
from permit_mcp import register

names: list[str] = register(exclude=["check_permission"])
register(exclude=1)  # type: ignore[arg-type]
"""


def plant_wheel(dist: Path, api: str = API) -> None:
    """Write a wheel of a typed `permit_mcp` package whose __init__.py is `api` into dist."""
    info = "permit_mcp-1.0.0.dist-info"
    files = {
        "permit_mcp/__init__.py": api,
        "permit_mcp/py.typed": "",
        f"{info}/METADATA": "Metadata-Version: 2.4\nName: permit-mcp\nVersion: 1.0.0\n",
        f"{info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: planted\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    record = []
    for name, text in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=")
        record.append(f"{name},sha256={digest.decode()},{len(text.encode())}")
    files[f"{info}/RECORD"] = "\n".join([*record, f"{info}/RECORD,,"]) + "\n"
    dist.mkdir(exist_ok=True)
    with zipfile.ZipFile(dist / "permit_mcp-1.0.0-py3-none-any.whl", "w") as wheel:
        for name, text in files.items():
            wheel.writestr(name, text)


def check(tmp_path: Path, fixture: str, api: str = API) -> tuple[int, str]:
    """Run the script on a planted wheel of `api` and a planted `fixture`."""
    plant_wheel(tmp_path / "dist", api)
    (tmp_path / "fixture.py").write_text(fixture)
    completed = run_script(tmp_path, SCRIPT, tmp_path / "dist", "fixture.py", cwd=tmp_path)
    return completed.returncode, completed.stdout + completed.stderr


def test_a_fixture_that_type_checks_against_the_wheel_passes(tmp_path: Path) -> None:
    status, output = check(tmp_path, FIXTURE)
    assert status == 0, output
    assert "The public API type-checks as fixture.py uses it." in output
    assert not list(tmp_path.glob("consumer-types.*")), "the work directory is removed"


def test_the_wheel_is_checked_not_the_checkout_or_an_editable_install(tmp_path: Path) -> None:
    # A permit_mcp in the directory the script runs from must not be seen. Nor may one in
    # mypy's own environment, such as the editable install of `uv sync`: it has no `register`.
    for package in (tmp_path / "permit_mcp", tmp_path / "src" / "permit_mcp"):
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(LOOSER_API)
        (package / "py.typed").write_text("")
    status, output = check(tmp_path, FIXTURE)
    assert status == 0, output


@pytest.mark.parametrize(
    ("line", "code"),
    [
        ("register(exclude=2)\n", "arg-type"),
        ('from permit_mcp import add\n\nadd(["x"])\n', "deprecated"),
        ("def untyped(x):\n    return x\n", "no-untyped-def"),
        ("import permit_mcp\n\nif permit_mcp:\n    pass\n", "truthy-bool"),
    ],
    ids=["an error", "a deprecated call", "an untyped function", "a code pyproject enables"],
)
def test_a_planted_error_fails(tmp_path: Path, line: str, code: str) -> None:
    status, output = check(tmp_path, FIXTURE + line)
    assert status == 1, output
    assert re.search(rf"^fixture\.py:\d+: error: .*\[{code}\]$", output, re.MULTILINE), output
    assert FAILED in output


def test_a_misuse_that_starts_to_type_check_fails_as_an_unused_ignore(tmp_path: Path) -> None:
    status, output = check(tmp_path, FIXTURE, api=LOOSER_API)
    assert status == 1, output
    assert 'fixture.py:4: error: Unused "type: ignore" comment  [unused-ignore]' in output
    assert FAILED in output


def test_an_ignore_must_name_the_exact_code(tmp_path: Path) -> None:
    fixture = FIXTURE.replace("ignore[arg-type]", "ignore[call-arg]")
    status, output = check(tmp_path, fixture)
    assert status == 1, output
    assert "fixture.py:4: error:" in output


@pytest.mark.parametrize("wheels", [0, 2])
def test_other_than_one_wheel_did_not_run(tmp_path: Path, wheels: int) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    for number in range(wheels):
        (dist / f"permit_mcp-1.0.{number}-py3-none-any.whl").write_text("")
    (tmp_path / "fixture.py").write_text(FIXTURE)
    completed = run_script(tmp_path, SCRIPT, dist, "fixture.py", cwd=tmp_path)
    assert completed.returncode == 2
    assert f"{DID_NOT_RUN} expected one wheel in {dist}, found {wheels}" in completed.stdout


def test_a_missing_fixture_did_not_run(tmp_path: Path) -> None:
    plant_wheel(tmp_path / "dist")
    completed = run_script(tmp_path, SCRIPT, tmp_path / "dist", "absent.py", cwd=tmp_path)
    assert completed.returncode == 2
    assert f"{DID_NOT_RUN} no fixture at absent.py" in completed.stdout


@pytest.mark.parametrize(("present", "missing"), [((), "mypy"), (("mypy",), "uv")])
def test_a_missing_tool_did_not_run(tmp_path: Path, present: tuple[str, ...], missing: str) -> None:
    plant_wheel(tmp_path / "dist")
    (tmp_path / "fixture.py").write_text(FIXTURE)
    (tmp_path / "bin").mkdir()
    for name in present:
        (tmp_path / "bin" / name).symlink_to(tool(name))
    completed = run_script(
        tmp_path,
        SCRIPT,
        tmp_path / "dist",
        "fixture.py",
        env={"PATH": str(tmp_path / "bin")},
        cwd=tmp_path,
    )
    assert completed.returncode == 2
    assert f"{DID_NOT_RUN} {missing} is not on PATH" in completed.stdout


def test_a_wheel_that_does_not_install_did_not_run(tmp_path: Path) -> None:
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "permit_mcp-1.0.0-py3-none-any.whl").write_text("not a zip")
    (tmp_path / "fixture.py").write_text(FIXTURE)
    completed = run_script(tmp_path, SCRIPT, tmp_path / "dist", "fixture.py", cwd=tmp_path)
    assert completed.returncode == 2
    assert f"{DID_NOT_RUN} uv could not install" in completed.stdout


def test_the_check_enables_the_error_codes_pyproject_enables() -> None:
    with (REPO_ROOT / "pyproject.toml").open("rb") as file:
        enabled = tomllib.load(file)["tool"]["mypy"]["enable_error_code"]
    script = (SCRIPTS / SCRIPT).read_text()
    listed = re.search(r"^enable_error_code =\n((?:    .*\n)+)", script, re.MULTILINE)
    assert listed is not None
    assert [code.strip(" ,") for code in listed[1].splitlines()] == enabled


def test_the_default_fixture_exists() -> None:
    assert (REPO_ROOT / "tests" / "consumer" / "consumer.py").is_file()
    assert "fixture=${2:-tests/consumer/consumer.py}" in (SCRIPTS / SCRIPT).read_text()
