"""How requests reach Permit: proxies, netrc, redirects, timeouts and cookies."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest
from werkzeug import Response

from permit_mcp import permit_api
from tests.api_record import CONTROL_PLANE, note_origin
from tests.support import (
    CASES,
    ELEMENT_AUTH,
    FACTS,
    OA_PATH,
    SCOPE_PATH,
    base_url,
    call_tool,
    error_text,
    login_call,
    payload,
    recorded,
    recorded_requests,
    serve_case,
    serve_json,
    settings_for,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_httpserver import HTTPServer
    from werkzeug import Request

    from permit_mcp import Settings

INSTANCES = CASES["list_resource_instances"]
# Nothing listens on the discard port, so a request sent there fails locally.
UNUSED_PROXY = "http://127.0.0.1:9"


async def test_http_proxy_from_the_environment_is_used(
    api: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_case(api, INSTANCES)
    monkeypatch.setenv("HTTP_PROXY", base_url(api))
    # A name under .invalid never resolves, so only the proxy can answer for it.
    settings = settings_for("http://permit.invalid")
    note_origin(settings.api_url, CONTROL_PLANE)

    result = await call_tool(settings, "list_resource_instances", INSTANCES.arguments)

    assert payload(result) == INSTANCES.expected
    requests = recorded_requests(api)
    assert [request.path for request in requests] == [SCOPE_PATH, f"{FACTS}/resource_instances"]
    assert {request.host for request in requests} == {"permit.invalid"}
    assert all(
        str(request.environ["RAW_URI"]).startswith("http://permit.invalid/") for request in requests
    )


async def test_no_proxy_bypasses_the_proxy(
    api: HTTPServer, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_case(api, INSTANCES)
    monkeypatch.setenv("HTTP_PROXY", UNUSED_PROXY)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")

    result = await call_tool(settings, "list_resource_instances", INSTANCES.arguments)

    assert payload(result) == INSTANCES.expected


async def test_unreachable_proxy_fails_the_call(
    api: HTTPServer, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_case(api, INSTANCES)
    monkeypatch.setenv("HTTP_PROXY", UNUSED_PROXY)

    result = await call_tool(settings, "list_resource_instances", INSTANCES.arguments)

    assert "could not reach 127.0.0.1" in error_text(result)
    assert api.log == []


async def test_netrc_is_not_read(
    api: HTTPServer, settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    netrc = tmp_path / "netrc"
    netrc.write_text("default login someone password something\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))
    serve_case(api, INSTANCES)

    result = await call_tool(settings, "list_resource_instances", INSTANCES.arguments)

    assert payload(result) == INSTANCES.expected
    assert recorded(api) == list(INSTANCES.calls)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_redirect_is_an_error_and_not_followed(
    api: HTTPServer, settings: Settings, status: int
) -> None:
    target = f"{base_url(api)}/elsewhere"
    api.expect_request(f"{FACTS}/resource_instances").respond_with_data(
        "", status=status, headers={"Location": target}
    )
    api.expect_request("/elsewhere").respond_with_json({"data": []})

    result = await call_tool(settings, "list_resource_instances", INSTANCES.arguments)

    assert f"returned HTTP {status}" in error_text(result)
    assert recorded(api) == list(INSTANCES.calls)


async def test_timeout_is_reported_without_a_status(
    api: HTTPServer, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(permit_api, "REQUEST_TIMEOUT_SECONDS", 0.2)
    release = threading.Event()

    def stall(_request: Request) -> Response:
        release.wait(5)
        return Response("{}", mimetype="application/json")

    api.expect_request(f"{FACTS}/resource_instances").respond_with_handler(stall)
    try:
        result = await call_tool(settings, "list_resource_instances", INSTANCES.arguments)
    finally:
        release.set()

    text = error_text(result)
    assert "list resource instances failed: no response from 127.0.0.1:" in text
    assert "within 0.2 seconds" in text
    assert "HTTP" not in text


async def test_cookies_from_one_call_are_not_sent_by_the_next(api: HTTPServer) -> None:
    # A host name, not an IP address: cookie jars refuse cookies from IP addresses.
    settings = settings_for(f"http://localhost:{api.port}")
    note_origin(settings.api_url, CONTROL_PLANE)
    case = CASES["list_operation_approvals"]

    def login(_request: Request) -> Response:
        response = Response(
            '{"element_bearer_token": "element_token_SECRET9876543210"}',
            mimetype="application/json",
        )
        response.set_cookie("session", "from-login")
        return response

    api.expect_request(login_call().path, method="POST").respond_with_handler(login)
    serve_json(api, case.calls[-1], case.response)

    result = await call_tool(settings, "list_operation_approvals", case.arguments)

    assert payload(result) == case.expected
    requests = recorded_requests(api)
    assert [request.path for request in requests][-1] == OA_PATH
    assert requests[-1].headers.get("Authorization") == ELEMENT_AUTH
    assert all("Cookie" not in request.headers for request in requests)
