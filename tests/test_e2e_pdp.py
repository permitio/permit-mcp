"""The end-to-end suite's container PDP helpers (tests/e2e/pdp.py), offline.

A stand-in `docker` on PATH records each call, its arguments and the PDP_API_KEY in its
environment, and answers as each test sets it up; a local server plays the PDP's health
endpoint. They check the exact docker commands, that the key never reaches a command line
and is redacted from every error and log, that the health wait is bounded by elapsed time,
that the container is removed whatever happens, and that its log is shown on failure.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from tests.e2e.conftest import PDP_LOG_SECTION, pytest_runtest_makereport
from tests.e2e.pdp import (
    HEALTH_PATH,
    PDP_IMAGE,
    ContainerPdp,
    ContainerPdpError,
    docker,
    is_healthy,
    run_container_pdp,
    wait_until_healthy,
    without_health_checks,
)
from tests.support import base_url

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from pytest_httpserver import HTTPServer

ROOT = Path(__file__).resolve().parent.parent
ENV_KEY = "permit_key_PDPSECRET222"
NAME = "mcp-e2e-7-1-pdp"
CONTROL_PLANE = "https://api.example.test"
RUN = [
    "run",
    "--detach",
    "--name",
    NAME,
    "--publish",
    "127.0.0.1::7000",
    "--env",
    "PDP_API_KEY",
    "--env",
    f"PDP_CONTROL_PLANE={CONTROL_PLANE}",
    PDP_IMAGE,
]
PORT = ["port", NAME, "7000/tcp"]
REMOVE = ["rm", "--force", NAME]
LOGS = ["logs", NAME]
NOISY_LOG = "\n".join(
    [
        "INFO starting the PDP",
        "GET /healthy 503",
        "WARN Health check failed: horizon is not ready",
        "GET /health 503",
        "WARN Health check failed: horizon is not ready",
        f"ERROR fetching the policy with {ENV_KEY} failed: 401",
        "GET /healthy 503",
        "WARN Health check failed: horizon is not ready",
    ]
)
QUIET_LOG = (
    "INFO starting the PDP\n"
    "WARN Health check failed: horizon is not ready\n"
    "ERROR fetching the policy with [REDACTED] failed: 401"
)

# The stand-in docker: records the call, then answers from <dir>/<command>.json.
FAKE_DOCKER = """
import json, os, sys, time
from pathlib import Path

home = Path(os.environ["FAKE_DOCKER_DIR"])
with (home / "calls.jsonl").open("a") as calls:
    calls.write(json.dumps({"args": sys.argv[1:], "key": os.environ.get("PDP_API_KEY")}) + "\\n")
answer_file = home / f"{sys.argv[1]}.json"
answer = json.loads(answer_file.read_text()) if answer_file.exists() else {}
time.sleep(answer.get("sleep", 0))
sys.stdout.write(answer.get("stdout", ""))
sys.stdout.flush()
sys.stderr.write(answer.get("stderr", ""))
sys.exit(answer.get("status", 0))
"""


class FakeDocker:
    """The stand-in docker's answers and the calls it recorded."""

    def __init__(self, home: Path) -> None:
        self.home = home

    def answer(
        self, command: str, *, status: int = 0, stdout: str = "", stderr: str = "", sleep: float = 0
    ) -> None:
        answer = {"status": status, "stdout": stdout, "stderr": stderr, "sleep": sleep}
        (self.home / f"{command}.json").write_text(json.dumps(answer))

    def calls(self) -> list[list[str]]:
        return [call["args"] for call in self._records()]

    def keys(self) -> list[str | None]:
        return [call["key"] for call in self._records()]

    def _records(self) -> list[dict[str, Any]]:
        record = self.home / "calls.jsonl"
        if not record.exists():
            return []
        return [json.loads(line) for line in record.read_text().splitlines()]


def install_fake_docker(home: Path) -> Path:
    """Write the stand-in docker into `home`/bin and return that directory."""
    bin_dir = home / "bin"
    bin_dir.mkdir()
    script = bin_dir / "docker"
    script.write_text(f"#!{sys.executable}\n{FAKE_DOCKER}")
    script.chmod(0o755)
    return bin_dir


@pytest.fixture
def fake_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    bin_dir = install_fake_docker(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_DOCKER_DIR", str(tmp_path))
    monkeypatch.delenv("PDP_API_KEY", raising=False)
    return FakeDocker(tmp_path)


@pytest.fixture
def healthy_pdp(fake_docker: FakeDocker, httpserver: HTTPServer) -> HTTPServer:
    """Docker publishes the PDP at the local server, which answers 200 on the health path."""
    fake_docker.answer("run", stdout="0123abcd\n")
    fake_docker.answer("port", stdout=f"{base_url(httpserver).removeprefix('http://')}\n")
    httpserver.expect_request(HEALTH_PATH).respond_with_data("ok")
    return httpserver


def start(within: float = 5.0) -> AbstractContextManager[ContainerPdp]:
    return run_container_pdp(NAME, ENV_KEY, CONTROL_PLANE, health_within=within)


def closed_port_url() -> str:
    """Return a local URL that refuses connections."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def test_the_image_is_pinned_by_version_and_digest() -> None:
    assert re.fullmatch(r"permitio/pdp-v2:v?\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", PDP_IMAGE)


def test_the_pdp_runs_with_the_key_by_name_only_and_is_removed_after_the_block(
    fake_docker: FakeDocker, healthy_pdp: HTTPServer
) -> None:
    with start() as pdp:
        assert pdp == ContainerPdp(name=NAME, url=base_url(healthy_pdp))
        assert fake_docker.calls() == [RUN, PORT]
    assert fake_docker.calls() == [RUN, PORT, REMOVE]
    # The key reaches docker run's environment only, and no command line.
    assert fake_docker.keys() == [ENV_KEY, None, None]
    assert all(ENV_KEY not in " ".join(call) for call in fake_docker.calls())
    assert [request.path for request, _ in healthy_pdp.log] == [HEALTH_PATH]


@pytest.mark.usefixtures("healthy_pdp")
def test_a_failed_test_still_removes_the_container_and_keeps_its_error(
    fake_docker: FakeDocker,
) -> None:
    with pytest.raises(AssertionError, match="the test failed") as caught, start():
        raise AssertionError("the test failed")  # noqa: EM101 - the test's own failure
    assert fake_docker.calls()[-1] == REMOVE
    assert not hasattr(caught.value, "__notes__")


def test_a_pdp_that_never_gets_healthy_fails_with_its_quiet_redacted_log_and_is_removed(
    fake_docker: FakeDocker, httpserver: HTTPServer
) -> None:
    fake_docker.answer("port", stdout=f"{base_url(httpserver).removeprefix('http://')}\n")
    fake_docker.answer("logs", stdout=NOISY_LOG)
    httpserver.expect_request(HEALTH_PATH).respond_with_data("starting", status=503)
    started = time.monotonic()
    with pytest.raises(ContainerPdpError) as caught, start(within=0.5):
        pytest.fail("the block must not run when the PDP is not healthy")
    assert time.monotonic() - started < 5
    assert str(caught.value) == (
        f"The container PDP {NAME} did not answer 200 on {HEALTH_PATH} within 0.5 seconds."
        f" Its log, without health checks:\n{QUIET_LOG}"
    )
    assert fake_docker.calls() == [RUN, PORT, LOGS, REMOVE]
    assert len(httpserver.log) >= 1


def test_a_failed_docker_run_is_redacted_and_still_removes_the_container(
    fake_docker: FakeDocker,
) -> None:
    fake_docker.answer("run", status=125, stderr=f"docker: invalid key {ENV_KEY}.\n")
    with pytest.raises(ContainerPdpError) as caught, start():
        pytest.fail("the block must not run when docker run failed")
    assert str(caught.value) == (
        f"docker {' '.join(RUN)} failed with exit status 125: docker: invalid key [REDACTED]."
    )
    assert fake_docker.calls() == [RUN, REMOVE]


def test_docker_port_without_an_address_fails(fake_docker: FakeDocker) -> None:
    with pytest.raises(ContainerPdpError, match="printed no address"), start():
        pytest.fail("the block must not run without the PDP's address")
    assert fake_docker.calls() == [RUN, PORT, REMOVE]


@pytest.mark.usefixtures("healthy_pdp")
def test_a_failed_removal_after_a_clean_block_fails_the_run(fake_docker: FakeDocker) -> None:
    fake_docker.answer("rm", status=1, stderr="Error response from daemon: busy\n")
    with (
        pytest.raises(ContainerPdpError, match=r"rm --force mcp-e2e-7-1-pdp failed with exit"),
        start(),
    ):
        pass


@pytest.mark.usefixtures("healthy_pdp")
def test_a_failed_removal_after_a_failure_is_a_note_and_a_log_line(
    fake_docker: FakeDocker, caplog: pytest.LogCaptureFixture
) -> None:
    fake_docker.answer("rm", status=1, stderr="Error response from daemon: busy\n")
    with pytest.raises(AssertionError, match="the test failed") as caught, start():
        raise AssertionError("the test failed")  # noqa: EM101 - the test's own failure
    removal = (
        f"docker rm --force {NAME} failed with exit status 1: Error response from daemon: busy"
    )
    assert caught.value.__notes__ == [f"Removing the container PDP {NAME} failed too: {removal}"]
    assert f"Could not remove the container PDP {NAME}: {removal}" in caplog.text


@pytest.mark.usefixtures("healthy_pdp")
def test_a_container_already_gone_counts_as_removed(fake_docker: FakeDocker) -> None:
    fake_docker.answer("rm", status=1, stderr=f"Error: No such container: {NAME}\n")
    with start():
        pass
    assert fake_docker.calls()[-1] == REMOVE


def test_docker_missing_from_path_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(ContainerPdpError, match="docker is not on PATH"):
        docker("ps")


def test_a_docker_command_that_hangs_is_stopped(fake_docker: FakeDocker) -> None:
    fake_docker.answer("pull", sleep=10)
    started = time.monotonic()
    with pytest.raises(ContainerPdpError, match=r"docker pull x did not finish within 0.5 seconds"):
        docker("pull", "x", timeout=0.5)
    assert time.monotonic() - started < 5


def test_the_log_keeps_stderr_in_order(fake_docker: FakeDocker) -> None:
    fake_docker.answer("logs", stdout="out\n", stderr="err\n")
    assert ContainerPdp(NAME, "http://127.0.0.1:1").log() == "out\nerr"


def test_a_log_that_cannot_be_read_says_why(fake_docker: FakeDocker) -> None:
    fake_docker.answer("logs", status=1, stderr="Error: No such container\n")
    assert ContainerPdp(NAME, "http://127.0.0.1:1").log() == (
        "The container PDP's log could not be read: docker logs mcp-e2e-7-1-pdp failed with"
        " exit status 1: Error: No such container"
    )


def test_the_log_drops_health_checks_keeps_the_first_horizon_failure_and_the_tail() -> None:
    assert without_health_checks(NOISY_LOG) == QUIET_LOG
    numbered = "\n".join(f"line {index}" for index in range(10))
    assert without_health_checks(numbered, keep=3) == "line 7\nline 8\nline 9"


@pytest.mark.parametrize(("status", "healthy"), [(200, True), (503, False), (404, False)])
def test_only_a_200_on_the_health_path_is_healthy(
    httpserver: HTTPServer, status: int, *, healthy: bool
) -> None:
    httpserver.expect_request(HEALTH_PATH).respond_with_data("", status=status)
    assert is_healthy(base_url(httpserver)) is healthy


def test_a_refused_connection_is_not_healthy() -> None:
    assert is_healthy(closed_port_url()) is False


def test_the_health_probe_ignores_proxies(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("http_proxy", closed_port_url())
    httpserver.expect_request(HEALTH_PATH).respond_with_data("ok")
    assert is_healthy(base_url(httpserver)) is True


def test_the_health_wait_probes_until_healthy() -> None:
    answers = iter([False, False, True])
    probed: list[str] = []

    def probe(url: str) -> bool:
        probed.append(url)
        return next(answers)

    pdp = ContainerPdp(NAME, "http://127.0.0.1:1")
    assert wait_until_healthy(pdp, within=5, interval=0.01, probe=probe) < 5
    assert probed == [pdp.url] * 3


def test_the_health_wait_is_bounded_by_elapsed_time(fake_docker: FakeDocker) -> None:
    probes = 0

    def probe(_url: str) -> bool:
        nonlocal probes
        probes += 1
        return False

    started = time.monotonic()
    with pytest.raises(ContainerPdpError, match=r"did not answer 200 on /healthy within 0.3"):
        wait_until_healthy(
            ContainerPdp(NAME, "http://127.0.0.1:1"), within=0.3, interval=0.05, probe=probe
        )
    assert 0.2 <= time.monotonic() - started < 1.0
    assert probes > 2
    assert fake_docker.calls() == [LOGS]


def make_report(
    item_fixtures: dict[str, object], *, when: str, failed: bool
) -> list[tuple[str, str]]:
    """Run the report hook on a stand-in item and report; return the report's sections."""
    item = cast("pytest.Item", SimpleNamespace(funcargs=item_fixtures))
    sections: list[tuple[str, str]] = []
    report = SimpleNamespace(when=when, failed=failed, sections=sections)
    hook = pytest_runtest_makereport(item)
    next(hook)
    with pytest.raises(StopIteration) as done:
        hook.send(cast("pytest.TestReport", report))
    assert done.value.value is report
    return sections


def test_a_failed_test_that_used_the_container_pdp_reports_its_log(
    fake_docker: FakeDocker,
) -> None:
    fake_docker.answer("logs", stdout=NOISY_LOG)
    pdp = ContainerPdp(NAME, "http://127.0.0.1:1")
    sections = make_report({"container_pdp": pdp}, when="call", failed=True)
    assert sections == [(PDP_LOG_SECTION, QUIET_LOG)]
    assert fake_docker.calls() == [LOGS]


@pytest.mark.parametrize(
    ("fixtures", "when", "failed"),
    [
        ({"container_pdp": ContainerPdp(NAME, "http://127.0.0.1:1")}, "call", False),
        ({"container_pdp": ContainerPdp(NAME, "http://127.0.0.1:1")}, "teardown", True),
        ({"world": object()}, "call", True),
    ],
    ids=["passed", "teardown", "cloud-pdp-test"],
)
def test_other_reports_get_no_log(
    fake_docker: FakeDocker, fixtures: dict[str, object], when: str, *, failed: bool
) -> None:
    assert make_report(fixtures, when=when, failed=failed) == []
    assert fake_docker.calls() == []


def run_selected_e2e(environ: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run `pytest -m e2e tests/e2e` in a fresh process with only `environ` and HOME."""
    keep = {name: os.environ[name] for name in ("HOME",) if name in os.environ}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", "e2e", "tests/e2e"],
        cwd=ROOT,
        env={**keep, **environ},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_a_run_without_credentials_exits_2_before_starting_any_container(
    tmp_path: Path,
) -> None:
    bin_dir = install_fake_docker(tmp_path)
    completed = run_selected_e2e({"PATH": str(bin_dir), "FAKE_DOCKER_DIR": str(tmp_path)})
    output = completed.stdout + completed.stderr
    assert completed.returncode == 2, output
    assert "The end-to-end suite did not run: set PERMIT_E2E_PROJECT_API_KEY and" in output
    assert FakeDocker(tmp_path).calls() == []


def test_a_run_without_docker_exits_2_before_any_request(tmp_path: Path) -> None:
    completed = run_selected_e2e(
        {
            "PATH": str(tmp_path),
            "PERMIT_E2E_PROJECT_API_KEY": "permit_key_PROJECTSECRET000",
            "PERMIT_E2E_PROJECT_ID": "proj",
            # Were a request sent, it would be refused here, not reach Permit.
            "PERMIT_E2E_API_URL": closed_port_url(),
        }
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 2, output
    assert "The end-to-end suite did not run: docker is not on PATH" in output
    assert re.search(r"\b(passed|skipped|failed|error)\b", output) is None, output
