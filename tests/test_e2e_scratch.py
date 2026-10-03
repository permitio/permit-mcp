"""The end-to-end suite's own helpers (tests/e2e/scratch.py), offline, against a mock API.

They check that the sweep deletes only stale `mcp-e2e-*` environments and reports every
failed delete, that the scratch environment is deleted after a failed setup too and that a
404 counts as deleted, that a 429 is retried for as long as its Retry-After asks and no
longer, that polling is bounded, that the environment's key is masked in Actions, and that a
selected e2e run without credentials exits 2 without running anything.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from werkzeug import Request, Response

from tests.e2e.scratch import (
    AdminClient,
    PermitAdminError,
    PollTimeoutError,
    SweepError,
    mask_in_actions,
    poll,
    scratch_world,
)
from tests.support import base_url

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pytest_httpserver import HTTPServer

ROOT = Path(__file__).resolve().parent.parent
PROJECT_KEY = "permit_key_PROJECTSECRET000"
ENV_KEY = "permit_key_ENVSECRET111"
PROJECT = "proj"
ENVS = f"/v2/projects/{PROJECT}/envs"
ENV_ID = "env-id"
RUN = "7-1"


def env(key: str, age: timedelta, *, zone: bool = True) -> dict[str, str]:
    created = datetime.now(UTC) - age
    stamp = created.isoformat() if zone else created.replace(tzinfo=None).isoformat()
    return {"id": f"id-{key}", "key": key, "created_at": stamp}


STALE = env("mcp-e2e-41-1", timedelta(hours=2))
STALE_WITHOUT_ZONE = env("mcp-e2e-local-0a1b2c3d", timedelta(days=3), zone=False)
FRESH = env("mcp-e2e-42-1", timedelta(minutes=10))
PRODUCTION = env("production", timedelta(days=400))


def answer(request: Request) -> Response:
    """Answer the setup's creates with the IDs the world reads from them."""
    body = json.loads(request.get_data()) if request.get_data() else {}
    path = request.path
    result: Any = body
    if path == ENVS:
        result = {"id": ENV_ID, "key": body["key"]}
    elif path.startswith("/v2/api-key/"):
        result = {"secret": ENV_KEY}
    elif path.endswith("/resources"):
        result = {"id": "res-id", "roles": {key: {"id": f"role-{key}"} for key in body["roles"]}}
    elif path.endswith(("/roles", "/users")):
        result = {**body, "id": f"id-{body['key']}"}
    return Response(json.dumps(result), content_type="application/json")


def serve(
    server: HTTPServer, listed: list[dict[str, str]] | None = None, *, delete_status: int = 204
) -> None:
    server.expect_request(ENVS, method="GET").respond_with_json(listed or [])
    server.expect_request(re.compile(".*"), method="DELETE").respond_with_data(
        "", status=delete_status
    )
    server.expect_request(re.compile(".*/tenants/default"), method="GET").respond_with_json({})
    server.expect_request(re.compile(".*")).respond_with_handler(answer)


def sent(server: HTTPServer) -> list[tuple[str, str]]:
    return [(request.method, request.full_path.rstrip("?")) for request, _ in server.log]


def project(server: HTTPServer) -> AdminClient:
    return AdminClient(base_url(server), PROJECT_KEY, sleep=no_sleep)


def no_sleep(seconds: float) -> None:
    pytest.fail(f"slept {seconds} seconds where no retry was due")


LIST = ("GET", f"{ENVS}?page=1&per_page=100")
CREATE = ("POST", ENVS)
DELETE_OURS = ("DELETE", f"{ENVS}/{ENV_ID}")


def test_the_world_sweeps_stale_environments_then_deletes_only_its_own(
    httpserver: HTTPServer,
) -> None:
    serve(httpserver, [PRODUCTION, STALE, FRESH, STALE_WITHOUT_ZONE])
    masked: list[tuple[str, int]] = []

    def mask(key: str) -> None:
        masked.append((key, len(httpserver.log)))

    with scratch_world(project(httpserver), PROJECT, RUN, mask) as world:
        assert world.environment_key == f"mcp-e2e-{RUN}"
        assert world.api_key == ENV_KEY
        assert ENV_KEY not in repr(world)
        built = len(httpserver.log)
    assert sent(httpserver)[:5] == [
        LIST,
        ("DELETE", f"{ENVS}/id-mcp-e2e-41-1"),
        ("DELETE", f"{ENVS}/id-mcp-e2e-local-0a1b2c3d"),
        CREATE,
        ("GET", f"/v2/api-key/{PROJECT}/{ENV_ID}"),
    ]
    # The key is handed over as soon as it is read, before any request sends it.
    assert masked == [(ENV_KEY, 5)]
    after = sent(httpserver)[built:]
    assert after == [DELETE_OURS]
    deletes = [call for call in sent(httpserver) if call[0] == "DELETE"]
    assert len(deletes) == 3


def test_the_sweep_reads_every_page(httpserver: HTTPServer) -> None:
    page = [env(f"other-{index}", timedelta(days=1)) for index in range(100)]
    httpserver.expect_request(ENVS, query_string="page=1&per_page=100").respond_with_json(page)
    httpserver.expect_request(ENVS, query_string="page=2&per_page=100").respond_with_json([STALE])
    serve(httpserver)
    with scratch_world(project(httpserver), PROJECT, RUN, lambda _key: None):
        pass
    assert sent(httpserver)[:3] == [
        LIST,
        ("GET", f"{ENVS}?page=2&per_page=100"),
        ("DELETE", f"{ENVS}/id-mcp-e2e-41-1"),
    ]


def test_a_failed_setup_still_deletes_the_environment_and_names_no_key(
    httpserver: HTTPServer,
) -> None:
    httpserver.expect_request(re.compile(".*/users"), method="POST").respond_with_data(
        f"rejected {ENV_KEY}", status=500
    )
    serve(httpserver)
    with (
        pytest.raises(PermitAdminError) as caught,
        scratch_world(project(httpserver), PROJECT, RUN, lambda _key: None),
    ):
        pytest.fail("the block must not run when the setup failed")
    assert str(caught.value) == (
        "POST /v2/facts/proj/env-id/users failed with HTTP 500: rejected [REDACTED]"
    )
    assert sent(httpserver)[-1] == DELETE_OURS


@pytest.mark.parametrize("status", [204, 404], ids=["deleted", "already-gone"])
def test_an_environment_already_gone_counts_as_deleted(httpserver: HTTPServer, status: int) -> None:
    serve(httpserver, [STALE], delete_status=status)
    with scratch_world(project(httpserver), PROJECT, RUN, lambda _key: None):
        pass
    assert sent(httpserver)[-1] == DELETE_OURS


def test_a_failed_delete_of_the_environment_fails_the_run(httpserver: HTTPServer) -> None:
    httpserver.expect_request(f"{ENVS}/{ENV_ID}", method="DELETE").respond_with_data(
        "locked", status=409
    )
    serve(httpserver)
    with (
        pytest.raises(PermitAdminError, match=f"DELETE {ENVS}/{ENV_ID} failed with HTTP 409"),
        scratch_world(project(httpserver), PROJECT, RUN, lambda _key: None),
    ):
        pass


LOCKED = f"DELETE {ENVS}/{ENV_ID} failed with HTTP 409: locked"
TEST_FAILURE = "the test failed"


def test_a_failed_delete_after_a_failed_setup_keeps_the_setup_s_error(
    httpserver: HTTPServer, caplog: pytest.LogCaptureFixture
) -> None:
    httpserver.expect_request(re.compile(".*/users"), method="POST").respond_with_data(
        "rejected", status=500
    )
    httpserver.expect_request(f"{ENVS}/{ENV_ID}", method="DELETE").respond_with_data(
        "locked", status=409
    )
    serve(httpserver)
    with (
        pytest.raises(PermitAdminError) as caught,
        scratch_world(project(httpserver), PROJECT, RUN, lambda _key: None),
    ):
        pytest.fail("the block must not run when the setup failed")
    assert str(caught.value) == "POST /v2/facts/proj/env-id/users failed with HTTP 500: rejected"
    assert caught.value.__notes__ == [
        f"Deleting the scratch environment mcp-e2e-{RUN} failed too: {LOCKED}"
    ]
    assert sent(httpserver)[-1] == DELETE_OURS
    [record] = [record for record in caplog.records if record.levelname == "ERROR"]
    assert record.getMessage() == (
        f"Could not delete the scratch environment mcp-e2e-{RUN} after a failure; the next"
        f" run's sweep deletes it once it is an hour old: {LOCKED}"
    )


def test_a_failed_delete_after_a_failed_test_keeps_the_test_s_failure(
    httpserver: HTTPServer, caplog: pytest.LogCaptureFixture
) -> None:
    httpserver.expect_request(f"{ENVS}/{ENV_ID}", method="DELETE").respond_with_data(
        "locked", status=409
    )
    serve(httpserver)
    with (
        pytest.raises(AssertionError) as caught,
        scratch_world(project(httpserver), PROJECT, RUN, lambda _key: None),
    ):
        raise AssertionError(TEST_FAILURE)
    assert str(caught.value) == TEST_FAILURE
    assert caught.value.__notes__ == [
        f"Deleting the scratch environment mcp-e2e-{RUN} failed too: {LOCKED}"
    ]
    assert caught.value.__cause__ is None
    assert sent(httpserver)[-1] == DELETE_OURS
    assert LOCKED in caplog.text


def test_a_failed_test_whose_environment_is_deleted_carries_no_note(
    httpserver: HTTPServer, caplog: pytest.LogCaptureFixture
) -> None:
    serve(httpserver)
    with (
        pytest.raises(AssertionError) as caught,
        scratch_world(project(httpserver), PROJECT, RUN, lambda _key: None),
    ):
        raise AssertionError(TEST_FAILURE)
    assert str(caught.value) == TEST_FAILURE
    assert not hasattr(caught.value, "__notes__")
    assert sent(httpserver)[-1] == DELETE_OURS
    assert [record for record in caplog.records if record.levelname == "ERROR"] == []


def test_the_sweep_reports_every_failed_delete_and_creates_nothing(
    httpserver: HTTPServer,
) -> None:
    serve(httpserver, [STALE, STALE_WITHOUT_ZONE], delete_status=500)
    with (
        pytest.raises(SweepError) as caught,
        scratch_world(project(httpserver), PROJECT, RUN, lambda _key: None),
    ):
        pytest.fail("the block must not run when the sweep failed")
    assert "mcp-e2e-41-1: DELETE" in str(caught.value)
    assert "mcp-e2e-local-0a1b2c3d: DELETE" in str(caught.value)
    assert CREATE not in sent(httpserver)
    assert len(sent(httpserver)) == 3


@pytest.mark.parametrize(("header", "wait"), [("2", 2.0), (None, 1.0), ("soon", 1.0)])
def test_a_429_is_retried_after_its_retry_after(
    httpserver: HTTPServer, header: str | None, wait: float
) -> None:
    headers = {} if header is None else {"Retry-After": header}
    httpserver.expect_oneshot_request("/thing").respond_with_data("", status=429, headers=headers)
    httpserver.expect_request("/thing").respond_with_json({"ok": True})
    slept: list[float] = []
    client = AdminClient(base_url(httpserver), PROJECT_KEY, sleep=slept.append)
    assert client.request("GET", "/thing") == {"ok": True}
    assert slept == [wait]


def test_a_429_that_asks_for_longer_than_the_budget_fails(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/thing").respond_with_data(
        "", status=429, headers={"Retry-After": "61"}
    )
    client = AdminClient(base_url(httpserver), PROJECT_KEY, rate_limit_budget=60, sleep=no_sleep)
    with pytest.raises(PermitAdminError, match="still rate limited after 60 seconds"):
        client.request("GET", "/thing")
    assert len(httpserver.log) == 1


async def test_poll_gives_up_once_its_time_is_spent() -> None:
    fetches = 0

    async def fetch() -> int:
        nonlocal fetches
        fetches += 1
        return fetches

    started = time.monotonic()
    with pytest.raises(PollTimeoutError, match=r"the count was still \d+ after 0.3 seconds"):
        async with asyncio.timeout(5):
            await poll("the count", fetch, expected=-1, within=0.3, interval=0.05)
    assert 0.2 <= time.monotonic() - started < 1.0
    assert fetches > 2


class FakeCapture:
    def __init__(self) -> None:
        self.disabled = 0

    @contextmanager
    def global_and_fixture_disabled(self) -> Iterator[None]:
        self.disabled += 1
        yield


@pytest.mark.parametrize("with_capture", [True, False], ids=["captured", "no-capture-plugin"])
def test_the_key_is_masked_in_actions(
    capsys: pytest.CaptureFixture[str], *, with_capture: bool
) -> None:
    capture = FakeCapture() if with_capture else None
    mask_in_actions(ENV_KEY, {"GITHUB_ACTIONS": "true"}, capture)
    assert capsys.readouterr().out == f"::add-mask::{ENV_KEY}\n"
    if capture is not None:
        assert capture.disabled == 1


@pytest.mark.parametrize("environ", [{}, {"GITHUB_ACTIONS": "false"}])
def test_the_key_is_not_printed_outside_actions(
    capsys: pytest.CaptureFixture[str], environ: dict[str, str]
) -> None:
    capture = FakeCapture()
    mask_in_actions(ENV_KEY, environ, capture)
    assert capsys.readouterr().out == ""
    assert capture.disabled == 0


@pytest.mark.parametrize(
    ("environ", "missing"),
    [
        ({}, "PERMIT_E2E_PROJECT_API_KEY and PERMIT_E2E_PROJECT_ID"),
        (
            {"PERMIT_E2E_PROJECT_ID": "proj", "PERMIT_E2E_PROJECT_API_KEY": "  "},
            "PERMIT_E2E_PROJECT_API_KEY",
        ),
        ({"PERMIT_E2E_PROJECT_API_KEY": PROJECT_KEY}, "PERMIT_E2E_PROJECT_ID"),
    ],
    ids=["neither", "blank-key", "no-project"],
)
def test_a_selected_e2e_run_without_credentials_exits_2_and_says_it_did_not_run(
    environ: dict[str, str], missing: str
) -> None:
    keep = {name: os.environ[name] for name in ("PATH", "HOME") if name in os.environ}
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", "e2e", "tests/e2e"],
        cwd=ROOT,
        env={**keep, **environ},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 2, output
    assert f"The end-to-end suite did not run: set {missing} (" in output
    assert re.search(r"\b(passed|skipped|failed|error)\b", output) is None, output
    assert PROJECT_KEY not in output
