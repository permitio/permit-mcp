"""Tests for verify-release.sh, which checks a release as a consumer gets it from PyPI.

PyPI is a stand-in curl that serves files planted under RUNNER_TEMP/web, keyed by URL;
pypi-attestations and uv are stand-ins too, and the wheel's permit-mcp is a script the
stand-in uv writes.

Run with:
uv run --only-dev pytest -c .github/scripts/pytest.ini .github/scripts/test_verify_release.py
"""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from harness import run_script, stand_in

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable
    from pathlib import Path

VERSION = "1.2.3"
WHEEL = f"permit_mcp-{VERSION}-py3-none-any.whl"
SDIST = f"permit_mcp-{VERSION}.tar.gz"
JSON_URL = f"pypi.org/pypi/permit-mcp/{VERSION}/json"
REPOSITORY = "permitio/permit-mcp"
PUBLISHER = {
    "kind": "GitHub",
    "repository": REPOSITORY,
    "workflow": "release.yml",
    "environment": "pypi",
}

# Serves RUNNER_TEMP/web/<URL without https://>: 200 and the file, or 404. Beside the
# file, <file>.later holds how many more times to answer 404 first, <file>.first is
# served once in its place, and <file>.down makes curl fail with no response.
CURL = r"""
out="" url=""
while (($#)); do
  case $1 in
    --output) out=$2; shift 2 ;;
    --max-time | --retry | --proto | --proto-redir | --write-out) shift 2 ;;
    -*) shift ;;
    *) url=$1; shift ;;
  esac
done
echo "$url" >>"$RUNNER_TEMP/curl.log"
path=$RUNNER_TEMP/web/${url#https://}
if [[ -e $path.down ]]; then echo "curl: (7) Failed to connect" >&2; exit 7; fi
if [[ -e $path.later ]] && (($(<"$path.later") > 0)); then
  echo $(($(<"$path.later") - 1)) >"$path.later"
  echo "Not Found" >"$out"; printf 404; exit 0
fi
if [[ -f $path.first ]]; then mv "$path.first" "$out"; printf 200; exit 0; fi
if [[ -f $path ]]; then cp "$path" "$out"; printf 200; exit 0; fi
echo "Not Found" >"$out"; printf 404
"""

ATTESTATIONS = {
    "ok": 'echo "OK: ${*: -1}"',
    "fails": 'f=${*: -1}; echo "Verification failed for ${f##*/}: digest mismatch"; exit 1',
    "crashes": 'echo "Traceback (most recent call last):"; echo "TUFError: no network"; exit 1',
}
CONFIGURATION_ERROR = 'echo "permit-mcp: configuration error: no API key" >&2; exit 2'


@dataclass
class Release:
    """What PyPI and the dist artifact hold; by default, a release that verifies.

    `served` is what PyPI serves (the built files when None), `listed_digests` the
    SHA-256 PyPI lists where it differs from the served file's, and `provenance` the
    provenance PyPI holds per file where it differs from a good one: None for none,
    a string for raw text.
    """

    built: dict[str, bytes] = field(
        default_factory=lambda: {WHEEL: b"the wheel", SDIST: b"the sdist"}
    )
    served: dict[str, bytes] | None = None
    listed_digests: dict[str, str] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    attestations: str = "ok"
    permit_mcp: str = CONFIGURATION_ERROR


def provenance(publisher: dict[str, str] | None = None) -> dict[str, Any]:
    bundle = {"publisher": publisher or PUBLISHER, "attestations": [{"version": 1}]}
    return {"version": 1, "attestation_bundles": [bundle]}


def bundles(*bundles: tuple[dict[str, str], int]) -> dict[str, Any]:
    """A provenance of one bundle per (publisher, number of attestations)."""
    return {
        "version": 1,
        "attestation_bundles": [
            {"publisher": publisher, "attestations": [{"version": 1}] * count}
            for publisher, count in bundles
        ],
    }


def plant(tmp_path: Path, release: Release) -> Path:
    """Plant the dist artifact, PyPI and the stand-ins; return the dist directory."""
    dist = tmp_path / "dist"
    dist.mkdir()
    for name, content in release.built.items():
        (dist / name).write_bytes(content)
    web = tmp_path / "web"
    urls = []
    served = release.built if release.served is None else release.served
    for name, content in served.items():
        path = web / "files.pythonhosted.org" / "packages" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        digest = release.listed_digests.get(name, hashlib.sha256(content).hexdigest())
        urls.append(
            {
                "filename": name,
                "url": f"https://files.pythonhosted.org/packages/{name}",
                "digests": {"sha256": digest},
            }
        )
        integrity = web / "pypi.org" / "integrity" / "permit-mcp" / VERSION / name / "provenance"
        integrity.parent.mkdir(parents=True, exist_ok=True)
        held = release.provenance.get(name, provenance())
        if held is not None:
            integrity.write_text(held if isinstance(held, str) else json.dumps(held))
    (web / JSON_URL).parent.mkdir(parents=True, exist_ok=True)
    (web / JSON_URL).write_text(json.dumps({"info": {"version": VERSION}, "urls": urls}))
    stand_in(tmp_path, "curl", CURL)
    stand_in(
        tmp_path,
        "pypi-attestations",
        'echo "$*" >>"$RUNNER_TEMP/attestations.log"\n'
        'if [[ $1 == --version ]]; then echo "pypi-attestations 0.0.30"; exit 0; fi\n'
        f"{ATTESTATIONS[release.attestations]}",
    )
    # The stand-in uv's venv writes the wheel's permit-mcp, which records its environment.
    permit_mcp = f'#!/bin/bash\nenv >"{tmp_path}/smoke-env.txt"\n{release.permit_mcp}\n'
    stand_in(
        tmp_path,
        "uv",
        'echo "$*" >>"$RUNNER_TEMP/uv.log"\n'
        "if [[ $1 == venv ]]; then\n"
        "  venv=${*: -1}\n"
        '  mkdir -p "$venv/bin"\n'
        f'  printf %s {shlex.quote(permit_mcp)} >"$venv/bin/permit-mcp"\n'
        '  chmod +x "$venv/bin/permit-mcp"\n'
        "fi",
    )
    return dist


def inputs(**env: str) -> dict[str, str]:
    """The job's inputs for v1.2.3, with no wait unless env says otherwise."""
    return {
        "TAG": f"v{VERSION}",
        "REPOSITORY": REPOSITORY,
        "WAIT_SECONDS": "0",
        "POLL_SECONDS": "0",
        **env,
    }


def verify(
    tmp_path: Path, release: Release | None = None, **env: str
) -> subprocess.CompletedProcess[str]:
    """Run verify-release.sh on a planted release of v1.2.3."""
    dist = plant(tmp_path, release or Release())
    return run_script(tmp_path, "verify-release.sh", dist, env=inputs(**env))


def log(tmp_path: Path, name: str) -> list[str]:
    path = tmp_path / f"{name}.log"
    return path.read_text().splitlines() if path.exists() else []


def errors(completed: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in completed.stdout.splitlines() if line.startswith("::error")]


# --- a release that verifies ---------------------------------------------------------------


def test_a_release_pypi_serves_as_built_and_attested_verifies(tmp_path: Path) -> None:
    completed = verify(tmp_path)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert errors(completed) == []
    assert completed.stdout.rstrip().endswith(
        f"Verified permit-mcp {VERSION} on PyPI: the files build made, attested by"
        f" {REPOSITORY}'s release.yml, and the wheel runs."
    )
    work = tmp_path / "verify"
    assert log(tmp_path, "attestations") == [
        "--version",
        *[
            f"verify pypi --repository https://github.com/{REPOSITORY} --provenance-file"
            f" {work}/{name}.provenance.json {work}/pypi/{name}"
            for name in (WHEEL, SDIST)
        ],
    ]
    assert log(tmp_path, "curl") == [
        f"https://{JSON_URL}",
        f"https://files.pythonhosted.org/packages/{WHEEL}",
        f"https://files.pythonhosted.org/packages/{SDIST}",
        f"https://pypi.org/integrity/permit-mcp/{VERSION}/{WHEEL}/provenance",
        f"https://pypi.org/integrity/permit-mcp/{VERSION}/{SDIST}/provenance",
    ]
    assert log(tmp_path, "uv") == [
        f"venv --quiet {work}/venv",
        (
            f"pip install --quiet --python {work}/venv/bin/python --exclude-newer false"
            f" {work}/pypi/{WHEEL}"
        ),
    ], "the wheel PyPI serves is installed, not the artifact's"
    smoke_env = (tmp_path / "smoke-env.txt").read_text().splitlines()
    shell_own = ("PWD=", "SHLVL=", "_=", "OLDPWD=")
    assert [line for line in smoke_env if not line.startswith(shell_own)] == []


def test_the_check_waits_for_pypi_to_serve_the_version(tmp_path: Path) -> None:
    dist = plant(tmp_path, Release())
    (tmp_path / "web" / f"{JSON_URL}.later").write_text("3")
    completed = run_script(tmp_path, "verify-release.sh", dist, env=inputs(WAIT_SECONDS="60"))
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert log(tmp_path, "curl").count(f"https://{JSON_URL}") == 4


def test_the_check_waits_for_the_second_file(tmp_path: Path) -> None:
    dist = plant(tmp_path, Release())
    json_path = tmp_path / "web" / JSON_URL
    listed = json.loads(json_path.read_text())
    listed["urls"] = listed["urls"][:1]
    json_path.with_name("json.first").write_text(json.dumps(listed))
    completed = run_script(tmp_path, "verify-release.sh", dist, env=inputs(WAIT_SECONDS="60"))
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert log(tmp_path, "curl").count(f"https://{JSON_URL}") == 2


# --- mismatches: exit 1 --------------------------------------------------------------------

OTHER_WHEEL_SHA256 = hashlib.sha256(b"another wheel").hexdigest()
NOT_FROM = "are not all from GitHub, permitio/permit-mcp, release.yml and the pypi environment"


@pytest.mark.parametrize(
    ("release", "says"),
    [
        (Release(served={WHEEL: b"the wheel"}), f"PyPI lists {WHEEL} for permit-mcp {VERSION};"),
        (
            Release(served={WHEEL: b"the wheel", SDIST: b"the sdist", "extra.whl": b""}),
            f"PyPI lists extra.whl, {WHEEL}, {SDIST} for",
        ),
        (
            Release(served={WHEEL: b"another wheel", SDIST: b"the sdist"}),
            f"{WHEEL} differs: PyPI serves SHA-256 {OTHER_WHEEL_SHA256}",
        ),
        (
            Release(
                served={WHEEL: b"another wheel", SDIST: b"the sdist"},
                listed_digests={WHEEL: hashlib.sha256(b"the wheel").hexdigest()},
            ),
            f"{WHEEL} differs: PyPI serves SHA-256 {OTHER_WHEEL_SHA256}",
        ),
        (Release(listed_digests={SDIST: "0" * 64}), f"{SDIST} differs:"),
        (Release(provenance={SDIST: None}), f"PyPI holds no attestations for {SDIST}."),
        (Release(provenance={WHEEL: bundles()}), f"The attestations of {WHEEL} {NOT_FROM}"),
        (
            Release(provenance={WHEEL: bundles((PUBLISHER, 0))}),
            f"The attestations of {WHEEL} {NOT_FROM}",
        ),
        *[
            (
                Release(provenance={WHEEL: provenance({**PUBLISHER, key: value})}),
                f"The attestations of {WHEEL} {NOT_FROM}",
            )
            for key, value in [
                ("repository", "someone/permit-mcp"),
                ("workflow", "other.yml"),
                ("environment", "staging"),
                ("kind", "GitLab"),
            ]
        ],
        (
            Release(provenance={
                WHEEL: bundles((PUBLISHER, 1), ({**PUBLISHER, "workflow": "other.yml"}, 1))
            }),
            f"The attestations of {WHEEL} {NOT_FROM}",
        ),
        (Release(provenance={SDIST: "{not json"}), f"The attestations of {SDIST} {NOT_FROM}"),
        (Release(attestations="fails"), f"The attestations of {WHEEL} do not verify."),
        (Release(permit_mcp="exit 0"), "permit-mcp from PyPI's wheel exited 0 with no"),
        (Release(permit_mcp="echo boom >&2; exit 1"), "exited 1 with no configuration"),
        (Release(permit_mcp="echo usage >&2; exit 2"), "exited 2 with no configuration"),
    ],
    ids=[
        "missing sdist", "extra file", "served file differs", "served bytes not the listed",
        "listed digest differs", "no provenance", "no bundles", "a bundle without attestations",
        "another repository",
        "another workflow", "another environment", "another kind", "one bundle of two wrong",
        "unreadable provenance", "attestation does not verify", "permit-mcp exits 0",
        "permit-mcp exits 1", "exit 2 without a configuration error",
    ],
)  # fmt: skip
def test_a_release_pypi_serves_otherwise_than_built_and_attested_fails(
    tmp_path: Path, release: Release, says: str
) -> None:
    completed = verify(tmp_path, release, WAIT_SECONDS="1")
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert any(says in line for line in errors(completed)), completed.stdout
    assert "Verified permit-mcp" not in completed.stdout


def test_every_mismatch_is_reported_before_the_check_fails(tmp_path: Path) -> None:
    release = Release(
        served={WHEEL: b"another wheel", SDIST: b"the sdist"},
        attestations="fails",
        permit_mcp="exit 0",
    )
    completed = verify(tmp_path, release)
    assert completed.returncode == 1
    assert len(errors(completed)) == 4, completed.stdout


# --- the check did not run: exit 2 ---------------------------------------------------------


def test_pypi_never_serving_the_version_is_no_verdict(tmp_path: Path) -> None:
    dist = plant(tmp_path, Release())
    (tmp_path / "web" / JSON_URL).unlink()
    # bash's SECONDS ticks on whole wall-clock seconds, so a 1-second deadline can pass
    # right after the first request; 3 leaves at least 2 seconds of asking.
    completed = run_script(tmp_path, "verify-release.sh", dist, env=inputs(WAIT_SECONDS="3"))
    assert completed.returncode == 2, completed.stdout + completed.stderr
    never = f"PyPI did not serve permit-mcp {VERSION} within 3s (last answer: 404)."
    assert errors(completed) == [f"::error title=Verify::{never}"]
    asked = log(tmp_path, "curl")
    assert len(asked) > 1, "it asked until the deadline"
    assert set(asked) == {f"https://{JSON_URL}"}


def test_no_answer_from_pypi_is_no_verdict(tmp_path: Path) -> None:
    dist = plant(tmp_path, Release())
    (tmp_path / "web" / f"{JSON_URL}.down").write_text("")
    completed = run_script(tmp_path, "verify-release.sh", dist, env=inputs())
    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert "(last answer: no response)." in completed.stdout


PROVENANCE = f"web/pypi.org/integrity/permit-mcp/{VERSION}/{SDIST}/provenance"


@pytest.mark.parametrize(
    ("break_it", "says"),
    [
        (lambda tmp: (tmp / "dist" / SDIST).unlink(), f"dist holds {WHEEL}; expected"),
        (lambda tmp: (tmp / "dist" / ".extra").write_text(""), f"holds .extra, {WHEEL}"),
        (
            lambda tmp: (tmp / "web/files.pythonhosted.org/packages" / WHEEL).unlink(),
            f"Could not download {WHEEL} from PyPI (404).",
        ),
        (
            lambda tmp: (tmp / f"{PROVENANCE}.down").write_text(""),
            f"Could not download the provenance of {SDIST} (no response).",
        ),
    ],
    ids=["dist misses a file", "dist holds another", "a download fails", "provenance unreachable"],
)  # fmt: skip
def test_a_check_that_cannot_run_is_no_verdict(
    tmp_path: Path, break_it: Callable[[Path], object], says: str
) -> None:
    dist = plant(tmp_path, Release())
    break_it(tmp_path)
    completed = run_script(tmp_path, "verify-release.sh", dist, env=inputs())
    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert any(says in line for line in errors(completed)), completed.stdout


def test_pypi_attestations_that_does_not_run_stops_the_check_before_pypi(tmp_path: Path) -> None:
    dist = plant(tmp_path, Release())
    (tmp_path / "bin" / "pypi-attestations").write_text("#!/bin/bash\nexit 127\n")
    completed = run_script(tmp_path, "verify-release.sh", dist, env=inputs())
    assert completed.returncode == 2
    assert errors(completed) == ["::error title=Verify::pypi-attestations did not run."]
    assert log(tmp_path, "curl") == []


def test_pypi_attestations_failing_to_run_is_no_verdict(tmp_path: Path) -> None:
    completed = verify(tmp_path, Release(attestations="crashes"))
    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert errors(completed) == [
        f"::error title=Verify::pypi-attestations did not verify {WHEEL}; see the log."
    ]
    assert "TUFError: no network" in completed.stdout


def test_a_wheel_that_does_not_install_is_no_verdict(tmp_path: Path) -> None:
    dist = plant(tmp_path, Release())
    uv = tmp_path / "bin" / "uv"
    uv.write_text(uv.read_text().replace('echo "$*"', '[[ $1 == pip ]] && exit 1\necho "$*"'))
    completed = run_script(tmp_path, "verify-release.sh", dist, env=inputs())
    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert errors(completed) == [
        f"::error title=Verify::Could not install {WHEEL} from PyPI in a fresh environment."
    ]


@pytest.mark.parametrize("unset", ["TAG", "REPOSITORY", "WAIT_SECONDS", "POLL_SECONDS"])
def test_an_unset_input_is_no_verdict(tmp_path: Path, unset: str) -> None:
    dist = plant(tmp_path, Release())
    env = inputs()
    del env[unset]
    completed = run_script(tmp_path, "verify-release.sh", dist, env=env)
    assert completed.returncode == 2
    assert completed.stdout == f"::error title=verify-release.sh::did not run: {unset} unset\n"
