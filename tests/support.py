"""Constants and helpers shared by the offline test suite.

The Permit API and the PDP are local pytest-httpserver instances. Every helper here works on
what crossed the wire (method, raw path, query, JSON body, Authorization header) or on what an
MCP client received, never on the package's internals.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, quote, unquote

from mcp.client import Client
from mcp.types import CallToolResult, TextContent

from permit_mcp import Settings, bound_user, create_server

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from pytest_httpserver import HTTPServer
    from werkzeug import Request

    from permit_mcp import IdentityResolver

API_KEY = "permit_key_TESTSECRET0123456789"
ELEMENT_TOKEN = "element_token_SECRET9876543210"  # noqa: S105 - a fake value served by the mock
# The redirect-login token login_as also returns; it must never be used as the Elements bearer.
REDIRECT_TOKEN = "redirect_token_NOTTHEBEARER42"  # noqa: S105 - a fake value served by the mock
USER = "alice"
TENANT = "acme"
RESOURCE = "documents"
AR_ELEMENT = "ar-elem"
OA_ELEMENT = "oa-elem"
AR_ID = "6f1c2a9e-3b7d-4c55-9a0e-1d2b3c4d5e6f"
OA_ID = "0b9e8d7c-6a5f-4e3d-8c2b-1a0f9e8d7c6b"

SCOPE_PATH = "/v2/api-key/scope"
SCOPE = {"organization_id": "org", "project_id": "proj-id", "environment_id": "env-id"}
FACTS = "/v2/facts/proj-id/env-id"
LOGIN_PATH = "/v2/auth/elements_login_as"
LOGIN_RESPONSE: dict[str, Any] = {
    "element_bearer_token": ELEMENT_TOKEN,
    "token": REDIRECT_TOKEN,
    "redirect_url": "https://app.permit.io/embed",
    "error": None,
    "error_code": None,
    "extra": None,
}
OA_PATH = f"/v2/elements/proj-id/env-id/config/{OA_ELEMENT}/operation_approval"
AR_ELEMENTS_PATH = f"/v2/elements/proj-id/env-id/config/{AR_ELEMENT}/access_requests"
PDP_PATH = "/allowed"
UNUSED_PDP_URL = "http://127.0.0.1:9"
API_AUTH = f"Bearer {API_KEY}"
ELEMENT_AUTH = f"Bearer {ELEMENT_TOKEN}"


def access_requests_path(user: str = USER) -> str:
    """Return the raw (escaped) access-request collection path for a user."""
    return f"{FACTS}/access_requests/{AR_ELEMENT}/user/{quote(user, safe='')}/tenant/{TENANT}"


def login_call(user: str = USER) -> Call:
    """Return the elements login_as request expected before every operation-approval call."""
    return Call("POST", LOGIN_PATH, body={"user_id": user, "tenant_id": TENANT})


@dataclass(frozen=True)
class Call:
    """One HTTP request as it crossed the wire; `body` None means no body was sent."""

    method: str
    path: str
    query: dict[str, str] = field(default_factory=dict)
    body: Any = None
    authorization: str = API_AUTH


def settings_for(
    api_url: str,
    *,
    pdp_url: str | None = None,
    user: str | None = None,
    access_request_element: str | None = AR_ELEMENT,
    operation_approval_element: str | None = OA_ELEMENT,
) -> Settings:
    """Build Settings that point every URL at local servers.

    Without `pdp_url` the PDP is an unused local port, so a stray check fails to connect.
    """
    return Settings(
        api_key=API_KEY,
        resource=RESOURCE,
        tenant=TENANT,
        api_url=api_url,
        pdp_url=UNUSED_PDP_URL if pdp_url is None else pdp_url,
        access_request_element=access_request_element,
        operation_approval_element=operation_approval_element,
        user=user,
    )


def base_url(server: HTTPServer) -> str:
    """Return the server's base URL without a trailing slash."""
    return f"http://{server.host}:{server.port}"


def serve_json(server: HTTPServer, call: Call, response: object, *, status: int = 200) -> None:
    """Answer requests matching the call's method and (decoded) path with JSON."""
    server.expect_request(unquote(call.path), method=call.method).respond_with_json(
        response, status=status
    )


def serve_data(server: HTTPServer, call: Call, data: str, *, status: int) -> None:
    """Answer requests matching the call's method and (decoded) path with a raw body."""
    server.expect_request(unquote(call.path), method=call.method).respond_with_data(
        data, status=status
    )


def to_call(request: Request) -> Call:
    """Convert a request recorded by pytest-httpserver into a Call."""
    raw_uri = str(request.environ["RAW_URI"])
    raw_path = raw_uri.split("?", 1)[0]
    pairs = parse_qsl(request.query_string.decode(), keep_blank_values=True)
    query = dict(pairs)
    assert len(query) == len(pairs), f"repeated query parameter in {raw_uri}"
    data = request.get_data()
    body = json.loads(data) if data else None
    return Call(
        method=request.method,
        path=raw_path,
        query=query,
        body=body,
        authorization=request.headers.get("Authorization", ""),
    )


def recorded(server: HTTPServer) -> list[Call]:
    """Return the requests the server received, excluding the API-key scope lookup."""
    calls = [to_call(request) for request, _ in server.log]
    return [call for call in calls if call.path != SCOPE_PATH]


def recorded_requests(server: HTTPServer) -> list[Request]:
    """Return every raw request the server received, including the scope lookup."""
    return [request for request, _ in server.log]


def wire_text(server: HTTPServer) -> str:
    """Return everything the client sent (URIs, headers, bodies) as one string."""
    parts: list[str] = []
    for request, _ in server.log:
        parts.append(str(request.environ["RAW_URI"]))
        parts.append(unquote(str(request.environ["RAW_URI"])))
        parts.extend(f"{name}: {value}" for name, value in request.headers.items())
        parts.append(request.get_data(as_text=True))
    return "\n".join(parts)


def text_of(result: CallToolResult) -> str:
    """Return the concatenated text content of a tool result."""
    return "\n".join(block.text for block in result.content if isinstance(block, TextContent))


def payload(result: CallToolResult) -> Any:  # noqa: ANN401 - JSON from the tool is untyped
    """Return the JSON value a successful tool call produced.

    The text content is the JSON form of the tool's return value; when the server also sends
    structured content it must carry the same value (possibly wrapped in {"result": ...}).
    """
    texts = [block.text for block in result.content if isinstance(block, TextContent)]
    assert not result.is_error, texts
    assert len(texts) == 1, texts
    value = json.loads(texts[0])
    if result.structured_content is not None:
        assert result.structured_content in (value, {"result": value})
    return value


def error_text(result: CallToolResult) -> str:
    """Assert the tool call failed and return its error text."""
    assert result.is_error, result
    return text_of(result)


@asynccontextmanager
async def connected(
    settings: Settings, identity: IdentityResolver | None = None
) -> AsyncIterator[Client]:
    """Build the server with a bound identity (alice by default) and open an MCP client."""
    server = create_server(settings, identity=identity or bound_user(USER))
    async with Client(server) as client:
        yield client


async def call_tool(
    settings: Settings,
    name: str,
    arguments: Mapping[str, Any],
    identity: IdentityResolver | None = None,
) -> CallToolResult:
    """Call one tool on a fresh server and return its result."""
    async with connected(settings, identity) as client:
        return await client.call_tool(name, dict(arguments))


AR_PATH = access_requests_path()
ACCESS_REQUEST = {"id": AR_ID, "status": "pending", "role": "editor", "tenant": TENANT}
OPERATION_APPROVAL = {"id": OA_ID, "status": "pending", "tenant": TENANT}
ENVELOPE_ITEMS = [{"id": AR_ID, "status": "pending"}]


@dataclass(frozen=True)
class Case:
    """One tool call: its arguments, the requests it must send, and what it must return.

    For the tools that use an Elements token (the operation-approval tools and
    cancel_access_request) `calls` starts with the elements login_as request, which
    the mock answers with LOGIN_RESPONSE; the last call is answered with `response`. When
    `pdp` is True the calls go to the mock PDP, and nothing may reach the mock API.
    """

    arguments: dict[str, Any]
    calls: tuple[Call, ...]
    response: Any
    expected: Any
    pdp: bool = False


def server_of(case: Case, api: HTTPServer, pdp: HTTPServer) -> HTTPServer:
    """Return the mock that the case's calls go to."""
    return pdp if case.pdp else api


CASES: dict[str, Case] = {
    "list_resource_instances": Case(
        arguments={"page": 2, "per_page": 10},
        calls=(
            Call(
                "GET",
                f"{FACTS}/resource_instances",
                query={
                    "tenant": TENANT,
                    "resource": RESOURCE,
                    "page": "2",
                    "per_page": "10",
                },
            ),
        ),
        response={"data": [{"key": "doc-1", "resource": RESOURCE}], "total_count": 1},
        expected={"data": [{"key": "doc-1", "resource": RESOURCE}], "total_count": 1},
    ),
    "check_permission": Case(
        arguments={"action": "edit", "resource_instance": "doc-1"},
        calls=(
            Call(
                "POST",
                PDP_PATH,
                body={
                    "user": {"key": USER},
                    "action": "edit",
                    "resource": {"type": RESOURCE, "tenant": TENANT, "key": "doc-1"},
                    "context": {},
                },
            ),
        ),
        response={"allow": True, "result": True, "query": {}, "debug": {}},
        expected={"allowed": True},
        pdp=True,
    ),
    "create_access_request": Case(
        arguments={
            "role": "editor",
            "reason": "quarterly review",
            "resource_instance": "doc-1",
        },
        calls=(
            Call(
                "POST",
                AR_PATH,
                body={
                    "access_request_details": {
                        "tenant": TENANT,
                        "resource": RESOURCE,
                        "role": "editor",
                        "resource_instance": "doc-1",
                    },
                    "reason": "quarterly review",
                },
            ),
        ),
        response=ACCESS_REQUEST,
        expected={"status": "created", "access_request": ACCESS_REQUEST},
    ),
    "list_access_requests": Case(
        arguments={
            "status": "pending",
            "role": "editor",
            "resource_instance": "doc-1",
            "page": 3,
            "per_page": 5,
        },
        calls=(
            Call(
                "GET",
                AR_PATH,
                query={
                    "status": "pending",
                    "role": "editor",
                    "resource": RESOURCE,
                    "resource_instance_id": "doc-1",
                    "page": "3",
                    "per_page": "5",
                },
            ),
        ),
        response={"data": ENVELOPE_ITEMS, "total_count": 1, "page_count": 1},
        expected={"data": ENVELOPE_ITEMS, "total_count": 1, "page_count": 1},
    ),
    "approve_access_request": Case(
        arguments={"access_request_id": AR_ID, "reviewer_comment": "looks fine"},
        calls=(
            Call(
                "PUT",
                f"{AR_PATH}/{AR_ID}/approve",
                body={"reviewer_comment": "looks fine"},
            ),
        ),
        response={**ACCESS_REQUEST, "status": "approved"},
        expected={
            "status": "approved",
            "access_request": {**ACCESS_REQUEST, "status": "approved"},
        },
    ),
    "deny_access_request": Case(
        arguments={"access_request_id": AR_ID, "reviewer_comment": "not needed"},
        calls=(Call("PUT", f"{AR_PATH}/{AR_ID}/deny", body={"reviewer_comment": "not needed"}),),
        response={**ACCESS_REQUEST, "status": "denied"},
        expected={
            "status": "denied",
            "access_request": {**ACCESS_REQUEST, "status": "denied"},
        },
    ),
    "cancel_access_request": Case(
        arguments={"access_request_id": AR_ID},
        calls=(
            login_call(),
            Call("PUT", f"{AR_ELEMENTS_PATH}/{AR_ID}/cancel", authorization=ELEMENT_AUTH),
        ),
        response={**ACCESS_REQUEST, "status": "canceled"},
        expected={
            "status": "canceled",
            "access_request": {**ACCESS_REQUEST, "status": "canceled"},
        },
    ),
    "create_operation_approval": Case(
        arguments={"reason": "one-off export", "resource_instance": "doc-1"},
        calls=(
            login_call(),
            Call(
                "POST",
                OA_PATH,
                body={
                    "access_request_details": {
                        "tenant": TENANT,
                        "resource": RESOURCE,
                        "resource_instance": "doc-1",
                    },
                    "reason": "one-off export",
                },
                authorization=ELEMENT_AUTH,
            ),
        ),
        response=OPERATION_APPROVAL,
        expected={"status": "created", "operation_approval": OPERATION_APPROVAL},
    ),
    "list_operation_approvals": Case(
        arguments={
            "status": "pending",
            "resource_instance": "doc-1",
            "page": 2,
            "per_page": 50,
        },
        calls=(
            login_call(),
            Call(
                "GET",
                OA_PATH,
                query={
                    "resource": RESOURCE,
                    "status": "pending",
                    "resource_instance": "doc-1",
                    "page": "2",
                    "per_page": "50",
                },
                authorization=ELEMENT_AUTH,
            ),
        ),
        response={"data": [OPERATION_APPROVAL], "total_count": 1, "page_count": 1},
        expected={"data": [OPERATION_APPROVAL], "total_count": 1, "page_count": 1},
    ),
    "approve_operation_approval": Case(
        arguments={"operation_approval_id": OA_ID, "reviewer_comment": "go ahead"},
        calls=(
            login_call(),
            Call(
                "PUT",
                f"{OA_PATH}/{OA_ID}/approve",
                body={"reviewer_comment": "go ahead"},
                authorization=ELEMENT_AUTH,
            ),
        ),
        response={**OPERATION_APPROVAL, "status": "approved"},
        expected={
            "status": "approved",
            "operation_approval": {**OPERATION_APPROVAL, "status": "approved"},
        },
    ),
    "deny_operation_approval": Case(
        arguments={"operation_approval_id": OA_ID, "reviewer_comment": "too risky"},
        calls=(
            login_call(),
            Call(
                "PUT",
                f"{OA_PATH}/{OA_ID}/deny",
                body={"reviewer_comment": "too risky"},
                authorization=ELEMENT_AUTH,
            ),
        ),
        response={**OPERATION_APPROVAL, "status": "denied"},
        expected={
            "status": "denied",
            "operation_approval": {**OPERATION_APPROVAL, "status": "denied"},
        },
    ),
    "cancel_operation_approval": Case(
        arguments={"operation_approval_id": OA_ID},
        calls=(
            login_call(),
            Call("PUT", f"{OA_PATH}/{OA_ID}/cancel", authorization=ELEMENT_AUTH),
        ),
        response={**OPERATION_APPROVAL, "status": "canceled"},
        expected={
            "status": "canceled",
            "operation_approval": {**OPERATION_APPROVAL, "status": "canceled"},
        },
    ),
}

ELEMENTS_TOKEN_TOOLS = sorted(name for name, case in CASES.items() if case.calls[0] == login_call())
APPROVE_DENY_TOOLS = sorted(name for name in CASES if name.startswith(("approve_", "deny_")))
# The tools that take the ID of one request.
BY_ID_TOOLS = sorted(name for name in CASES if name.startswith(("approve_", "deny_", "cancel_")))


def serve_case(api: HTTPServer, case: Case, *, status: int = 200, response: object = None) -> None:
    """Register the login handler (if any) and the final handler of a case."""
    *before, last = case.calls
    for call in before:
        assert call.path == LOGIN_PATH
        serve_json(api, call, LOGIN_RESPONSE)
    serve_json(api, last, case.response if response is None else response, status=status)
