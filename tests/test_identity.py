"""The acting user comes from the identity bound in code, never from tool arguments."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver import Context
from werkzeug import Response

from permit_mcp import IdentityError, access_token_subject, bound_user
from tests.support import (
    CASES,
    LOGIN_PATH,
    LOGIN_RESPONSE,
    access_requests_path,
    call_tool,
    connected,
    error_text,
    login_call,
    payload,
    recorded,
    serve_case,
    serve_json,
    wire_text,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pytest_httpserver import HTTPServer
    from werkzeug import Request

    from permit_mcp import IdentityResolver, Settings

ARGUMENTS_BY_TOOL = {
    "list_resource_instances": {"page", "per_page"},
    "create_access_request": {"role", "reason", "resource_instance"},
    "list_access_requests": {"status", "role", "resource_instance", "page", "per_page"},
    "approve_access_request": {"access_request_id", "reviewer_comment"},
    "deny_access_request": {"access_request_id", "reviewer_comment"},
    "create_operation_approval": {"reason", "resource_instance"},
    "list_operation_approvals": {"status", "resource_instance", "page", "per_page"},
    "approve_operation_approval": {"operation_approval_id", "reviewer_comment"},
    "deny_operation_approval": {"operation_approval_id", "reviewer_comment"},
}
IDENTITY_WORDS = (
    "user",
    "requester",
    "requesting",
    "actor",
    "principal",
    "subject",
    "identity",
)
INJECTED = "other-user"


class FixedResult:
    """A resolver that returns a fixed (possibly invalid) value."""

    def __init__(self, value: object) -> None:
        self.value = value

    async def __call__(self, ctx: Context[Any, Any]) -> str:
        del ctx
        return cast("str", self.value)


class Raises:
    """A resolver that raises the given exception."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def __call__(self, ctx: Context[Any, Any]) -> str:
        del ctx
        raise self.error


FAILING_RESOLVERS: dict[str, IdentityResolver] = {
    "identity-error": Raises(IdentityError("no session")),
    "unexpected-error": Raises(RuntimeError("resolver crashed")),
    "empty-string": FixedResult(""),
    "none": FixedResult(None),
    "not-a-string": FixedResult(42),
}


@contextmanager
def signed_in(subject: str | None) -> Iterator[None]:
    """Make the current request carry an access token with the given subject."""
    token = AccessToken(
        token="opaque",  # noqa: S106 - a fake token; nothing verifies it in-process
        client_id="client",
        scopes=[],
        subject=subject,
    )
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(reset)


async def test_no_tool_accepts_an_acting_user(settings: Settings) -> None:
    async with connected(settings) as client:
        tools = (await client.list_tools()).tools

    schemas = {tool.name: tool.input_schema for tool in tools}
    assert {name: set(schema.get("properties", {})) for name, schema in schemas.items()} == (
        ARGUMENTS_BY_TOOL
    )
    for name, schema in schemas.items():
        for prop in schema.get("properties", {}):
            assert not any(word in prop.lower() for word in IDENTITY_WORDS), (
                name,
                prop,
            )
        for prop in schema.get("required", []):
            assert not any(word in prop.lower() for word in IDENTITY_WORDS), (
                name,
                prop,
            )


@pytest.mark.parametrize("name", sorted(CASES))
async def test_injected_user_argument_never_reaches_the_api(
    api: HTTPServer, settings: Settings, name: str
) -> None:
    case = CASES[name]
    serve_case(api, case)
    arguments = {
        **case.arguments,
        "user_id": INJECTED,
        "user": INJECTED,
        "user_key": INJECTED,
    }

    result = await call_tool(settings, name, arguments)

    assert INJECTED not in wire_text(api)
    if result.is_error:
        assert recorded(api) == []
    else:
        assert payload(result) == case.expected
        assert recorded(api) == list(case.calls)


@pytest.mark.parametrize("resolver_id", sorted(FAILING_RESOLVERS))
@pytest.mark.parametrize("name", sorted(CASES))
async def test_unidentified_caller_fails_closed_without_any_request(
    api: HTTPServer, settings: Settings, name: str, resolver_id: str
) -> None:
    case = CASES[name]
    serve_case(api, case)

    result = await call_tool(settings, name, case.arguments, FAILING_RESOLVERS[resolver_id])

    assert "identif" in error_text(result).lower()
    assert api.log == []


async def test_bound_user_returns_its_key() -> None:
    assert await bound_user("alice")(Context()) == "alice"


@pytest.mark.parametrize("key", ["", "   ", "\t\n"], ids=["empty", "spaces", "whitespace"])
def test_bound_user_rejects_a_blank_key(key: str) -> None:
    with pytest.raises(ValueError, match="needs a non-empty Permit user key"):
        bound_user(key)


async def test_access_token_subject_without_a_token_raises() -> None:
    assert auth_context_var.get() is None
    with pytest.raises(IdentityError):
        await access_token_subject()(Context())


@pytest.mark.parametrize("subject", [None, ""])
async def test_access_token_without_a_subject_raises(subject: str | None) -> None:
    with signed_in(subject), pytest.raises(IdentityError):
        await access_token_subject()(Context())


async def test_access_token_subject_returns_the_subject() -> None:
    with signed_in("bob"):
        assert await access_token_subject()(Context()) == "bob"


async def test_tools_act_as_the_token_subject(api: HTTPServer, settings: Settings) -> None:
    case = CASES["list_access_requests"]
    listing = replace(case.calls[-1], path=access_requests_path("bob"))
    serve_json(api, listing, case.response)

    with signed_in("bob"):
        result = await call_tool(
            settings, "list_access_requests", case.arguments, access_token_subject()
        )

    assert payload(result) == case.expected
    assert recorded(api) == [listing]


async def test_operation_approvals_log_in_as_the_token_subject(
    api: HTTPServer, settings: Settings
) -> None:
    case = CASES["create_operation_approval"]
    serve_json(api, login_call("bob"), LOGIN_RESPONSE)
    serve_json(api, case.calls[-1], case.response)

    with signed_in("bob"):
        result = await call_tool(
            settings,
            "create_operation_approval",
            case.arguments,
            access_token_subject(),
        )

    assert payload(result) == case.expected
    assert recorded(api) == [login_call("bob"), case.calls[-1]]


async def test_tools_without_a_token_fail_closed(api: HTTPServer, settings: Settings) -> None:
    case = CASES["create_access_request"]
    serve_case(api, case)

    result = await call_tool(
        settings, "create_access_request", case.arguments, access_token_subject()
    )

    assert "identif" in error_text(result).lower()
    assert api.log == []


class InTurn:
    """A resolver that returns the given users, one per call, in order."""

    def __init__(self, *users: str) -> None:
        self.users = list(users)

    async def __call__(self, ctx: Context[Any, Any]) -> str:
        del ctx
        return self.users.pop(0)


def login_per_user(request: Request) -> Response:
    """Answer elements_login_as with a token that names the user who logged in."""
    user = request.get_json()["user_id"]
    login = {**LOGIN_RESPONSE, "element_bearer_token": f"token-of-{user}"}
    return Response(json.dumps(login), mimetype="application/json")


def serve_per_user(api: HTTPServer, users: tuple[str, ...]) -> None:
    """Serve create_access_request for each user, and operation approvals with per-user logins."""
    for user in users:
        serve_json(
            api,
            replace(CASES["create_access_request"].calls[-1], path=access_requests_path(user)),
            {},
        )
    api.expect_request(LOGIN_PATH, method="POST").respond_with_handler(login_per_user)
    serve_json(api, CASES["create_operation_approval"].calls[-1], {})


async def test_each_call_acts_as_the_user_resolved_for_it(
    api: HTTPServer, settings: Settings
) -> None:
    serve_per_user(api, ("alice", "bob"))
    access, operation = CASES["create_access_request"], CASES["create_operation_approval"]
    elements_call = operation.calls[-1]

    async with connected(settings, InTurn("alice", "bob", "alice", "bob")) as client:
        for _ in range(2):
            payload(await client.call_tool("create_access_request", access.arguments))
        for _ in range(2):
            payload(await client.call_tool("create_operation_approval", operation.arguments))

    assert recorded(api) == [
        replace(access.calls[-1], path=access_requests_path("alice")),
        replace(access.calls[-1], path=access_requests_path("bob")),
        login_call("alice"),
        replace(elements_call, authorization="Bearer token-of-alice"),
        login_call("bob"),
        replace(elements_call, authorization="Bearer token-of-bob"),
    ]


async def test_concurrent_calls_act_as_their_own_users(api: HTTPServer, settings: Settings) -> None:
    serve_per_user(api, ("alice", "bob"))
    access, operation = CASES["create_access_request"], CASES["create_operation_approval"]

    async with connected(settings, InTurn("alice", "bob", "alice", "bob")) as client:
        first = await asyncio.gather(
            *(client.call_tool("create_access_request", access.arguments) for _ in range(2))
        )
        second = await asyncio.gather(
            *(client.call_tool("create_operation_approval", operation.arguments) for _ in range(2))
        )

    for result in (*first, *second):
        payload(result)
    calls = recorded(api)
    assert sorted(call.path for call in calls[:2]) == sorted(
        [access_requests_path("alice"), access_requests_path("bob")]
    )
    logins = [call for call in calls[2:] if call.path == LOGIN_PATH]
    elements = [call for call in calls[2:] if call.path != LOGIN_PATH]
    assert sorted(call.body["user_id"] for call in logins) == ["alice", "bob"]
    assert sorted(call.authorization for call in elements) == [
        "Bearer token-of-alice",
        "Bearer token-of-bob",
    ]
