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
    ELEMENT_AUTH,
    LOGIN_PATH,
    LOGIN_RESPONSE,
    USER,
    Call,
    Case,
    access_requests_path,
    call_tool,
    connected,
    error_text,
    login_call,
    payload,
    recorded,
    serve_case,
    serve_json,
    server_of,
    wire_text,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pytest_httpserver import HTTPServer
    from werkzeug import Request

    from permit_mcp import IdentityResolver, Settings

ARGUMENTS_BY_TOOL = {
    "list_resource_instances": {"page", "per_page"},
    "check_permission": {"action", "resource_instance"},
    "create_access_request": {"role", "reason", "resource_instance"},
    "list_access_requests": {"status", "role", "resource_instance", "page", "per_page"},
    "approve_access_request": {"access_request_id", "reviewer_comment"},
    "deny_access_request": {"access_request_id", "reviewer_comment"},
    "cancel_access_request": {"access_request_id"},
    "create_operation_approval": {"reason", "resource_instance"},
    "list_operation_approvals": {"status", "resource_instance", "page", "per_page"},
    "approve_operation_approval": {"operation_approval_id", "reviewer_comment"},
    "deny_operation_approval": {"operation_approval_id", "reviewer_comment"},
    "cancel_operation_approval": {"operation_approval_id"},
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
    api: HTTPServer, pdp: HTTPServer, settings: Settings, name: str
) -> None:
    case = CASES[name]
    target = server_of(case, api, pdp)
    serve_case(target, case)
    arguments = {
        **case.arguments,
        "user_id": INJECTED,
        "user": INJECTED,
        "user_key": INJECTED,
    }

    result = await call_tool(settings, name, arguments)

    assert INJECTED not in wire_text(api)
    assert INJECTED not in wire_text(pdp)
    if result.is_error:
        assert recorded(api) == recorded(pdp) == []
    else:
        assert payload(result) == case.expected
        assert recorded(target) == list(case.calls)


@pytest.mark.parametrize("resolver_id", sorted(FAILING_RESOLVERS))
@pytest.mark.parametrize("name", sorted(CASES))
async def test_unidentified_caller_fails_closed_without_any_request(
    api: HTTPServer, pdp: HTTPServer, settings: Settings, name: str, resolver_id: str
) -> None:
    case = CASES[name]
    serve_case(server_of(case, api, pdp), case)

    result = await call_tool(settings, name, case.arguments, FAILING_RESOLVERS[resolver_id])

    assert "identif" in error_text(result).lower()
    assert api.log == []
    assert pdp.log == []


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


@pytest.mark.parametrize("name", ["create_operation_approval", "cancel_access_request"])
async def test_elements_calls_log_in_as_the_token_subject(
    api: HTTPServer, settings: Settings, name: str
) -> None:
    case = CASES[name]
    serve_json(api, login_call("bob"), LOGIN_RESPONSE)
    serve_json(api, case.calls[-1], case.response)

    with signed_in("bob"):
        result = await call_tool(settings, name, case.arguments, access_token_subject())

    assert payload(result) == case.expected
    assert recorded(api) == [login_call("bob"), case.calls[-1]]


async def test_permission_check_asks_about_the_token_subject(
    api: HTTPServer, pdp: HTTPServer, settings: Settings
) -> None:
    case = CASES["check_permission"]
    check = case.calls[-1]
    check = replace(check, body={**check.body, "user": {"key": "bob"}})
    serve_json(pdp, check, case.response)

    with signed_in("bob"):
        result = await call_tool(
            settings, "check_permission", case.arguments, access_token_subject()
        )

    assert payload(result) == case.expected
    assert recorded(pdp) == [check]
    assert api.log == []


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


def as_user(call: Call, user: str) -> Call:
    """Return `call` as a tool sends it when acting as `user` instead of USER.

    The user is in the login_as body, the access-request path, the Elements token the
    login returns (see `login_per_user`), and the PDP body.
    """
    if call.path == LOGIN_PATH:
        return login_call(user)
    path = call.path.replace(access_requests_path(USER), access_requests_path(user))
    authorization = (
        f"Bearer token-of-{user}" if call.authorization == ELEMENT_AUTH else call.authorization
    )
    body = call.body
    if isinstance(body, dict) and body.get("user") == {"key": USER}:
        body = {**body, "user": {"key": user}}
    return replace(call, path=path, authorization=authorization, body=body)


def calls_as(case: Case, user: str) -> list[Call]:
    """Return the requests the case's tool sends when acting as `user`."""
    return [as_user(call, user) for call in case.calls]


def serve_as_users(api: HTTPServer, pdp: HTTPServer, case: Case, users: tuple[str, ...]) -> None:
    """Serve the case for each of `users`, with logins that hand out per-user tokens."""
    api.expect_request(LOGIN_PATH, method="POST").respond_with_handler(login_per_user)
    for user in users:
        for call in calls_as(case, user):
            if call.path != LOGIN_PATH:
                serve_json(server_of(case, api, pdp), call, case.response)


@pytest.mark.parametrize("name", sorted(CASES))
async def test_each_call_acts_as_the_user_resolved_for_it(
    api: HTTPServer, pdp: HTTPServer, settings: Settings, name: str
) -> None:
    case = CASES[name]
    users = ("alice", "bob", "alice", "bob")
    serve_as_users(api, pdp, case, users)

    async with connected(settings, InTurn(*users)) as client:
        for _ in users:
            assert payload(await client.call_tool(name, case.arguments)) == case.expected

    expected = [call for user in users for call in calls_as(case, user)]
    assert recorded(server_of(case, api, pdp)) == expected


@pytest.mark.parametrize("name", sorted(CASES))
async def test_concurrent_calls_act_as_their_own_users(
    api: HTTPServer, pdp: HTTPServer, settings: Settings, name: str
) -> None:
    case = CASES[name]
    users = ("alice", "bob", "carol")
    serve_as_users(api, pdp, case, users)

    async with connected(settings, InTurn(*users)) as client:
        results = await asyncio.gather(*(client.call_tool(name, case.arguments) for _ in users))

    for result in results:
        assert payload(result) == case.expected
    expected = [call for user in users for call in calls_as(case, user)]
    sent = recorded(server_of(case, api, pdp))
    assert sorted(map(repr, sent)) == sorted(map(repr, expected))


@pytest.mark.parametrize("name", sorted(CASES))
async def test_the_resolver_wins_over_the_configured_user(
    api: HTTPServer, pdp: HTTPServer, settings: Settings, name: str
) -> None:
    case = CASES[name]
    serve_as_users(api, pdp, case, ("bob",))

    result = await call_tool(
        replace(settings, user="carol"), name, case.arguments, FixedResult("bob")
    )

    assert payload(result) == case.expected
    assert recorded(server_of(case, api, pdp)) == calls_as(case, "bob")
    assert "carol" not in wire_text(api)
    assert "carol" not in wire_text(pdp)
