"""create_server(auth=..., token_verifier=...) over streamable HTTP, driven by an MCP client."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl

from permit_mcp import ConfigError, bound_user, create_server
from tests.support import (
    CASES,
    access_requests_path,
    base_url,
    error_text,
    payload,
    recorded,
    serve_json,
    settings_for,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from mcp.server.mcpserver import MCPServer
    from pytest_httpserver import HTTPServer

    from permit_mcp import Settings

# The streamable HTTP app refuses a Host header without a port (DNS rebinding protection).
ORIGIN = "http://127.0.0.1:8000"
RESOURCE_URL = f"{ORIGIN}/mcp"
AUTH = AuthSettings(
    issuer_url=AnyHttpUrl("https://auth.example.com"),
    resource_server_url=AnyHttpUrl(RESOURCE_URL),
    validate_token_resource=True,
)
TOKENS = {
    "token-bob": "bob",
    "token-bob-renewed": "bob",
    "token-carol": "carol",
    "token-nobody": None,
}
# A protocol revision that opens a stateful session with the initialize handshake.
HANDSHAKE_VERSION = "2025-11-25"
JSON_ACCEPT = {"Accept": "application/json, text/event-stream"}
LISTING = CASES["list_access_requests"]
TOOL_CALL = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "list_access_requests", "arguments": {}},
}


class StubVerifier:
    """Accepts the tokens in `TOKENS`, each issued to its subject for `RESOURCE_URL`."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return the access token for a known token, or None."""
        self.seen.append(token)
        if token not in TOKENS:
            return None
        return AccessToken(
            token=token,
            client_id="host-client",
            scopes=[],
            resource=RESOURCE_URL,
            subject=TOKENS[token],
        )


@asynccontextmanager
async def http_to(
    server: MCPServer[Any], headers: Mapping[str, str], *, stateless: bool = True
) -> AsyncIterator[httpx2.AsyncClient]:
    """Serve the server's streamable HTTP app in-process, and yield an HTTP client for it."""
    app = server.streamable_http_app(stateless_http=stateless, json_response=True)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=ORIGIN, headers=dict(headers)
        ) as http,
    ):
        yield http


async def list_as(server: MCPServer[Any], token: str) -> Any:  # noqa: ANN401 - tool JSON
    """Call list_access_requests over HTTP with a bearer token, and return its result."""
    async with (
        http_to(server, {"Authorization": f"Bearer {token}"}) as http,
        Client(streamable_http_client(RESOURCE_URL, http_client=http)) as client,
    ):
        return await client.call_tool("list_access_requests", dict(LISTING.arguments))


def serve_listing(api: HTTPServer, user: str) -> None:
    serve_json(api, replace(LISTING.calls[-1], path=access_requests_path(user)), LISTING.response)


def authenticated_server(settings: Settings, verifier: StubVerifier) -> MCPServer[Any]:
    return create_server(settings, auth=AUTH, token_verifier=verifier)


async def test_each_token_acts_as_its_subject(api: HTTPServer) -> None:
    # PERMIT_MCP_USER is set, and still each call acts as its own token's subject.
    settings = settings_for(base_url(api), user="alice")
    serve_listing(api, "bob")
    serve_listing(api, "carol")
    verifier = StubVerifier()
    server = authenticated_server(settings, verifier)

    bob = await list_as(server, "token-bob")
    carol = await list_as(server, "token-carol")

    assert payload(bob) == payload(carol) == LISTING.expected
    assert [call.path for call in recorded(api)] == [
        access_requests_path("bob"),
        access_requests_path("carol"),
    ]
    assert "token-bob" in verifier.seen
    assert "token-carol" in verifier.seen


async def post_as(
    http: httpx2.AsyncClient, token: str, message: dict[str, Any], session_id: str | None
) -> httpx2.Response:
    """POST one JSON-RPC message with a bearer token, on a session when `session_id` is set."""
    headers = {**JSON_ACCEPT, "Authorization": f"Bearer {token}"}
    if session_id is not None:
        headers |= {"mcp-session-id": session_id, "mcp-protocol-version": HANDSHAKE_VERSION}
    return await http.post("/mcp", json=message, headers=headers)


async def test_a_session_serves_only_the_subject_that_opened_it(api: HTTPServer) -> None:
    # mcp's session manager binds a stateful session to the principal that opened it.
    settings = settings_for(base_url(api))
    serve_listing(api, "bob")
    serve_listing(api, "carol")
    server = authenticated_server(settings, StubVerifier())
    initialize = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": HANDSHAKE_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    call = {
        **TOOL_CALL,
        "params": {"name": "list_access_requests", "arguments": LISTING.arguments},
    }

    async with http_to(server, {}, stateless=False) as http:
        opened = await post_as(http, "token-bob", initialize, None)
        session_id = opened.headers["mcp-session-id"]
        await post_as(http, "token-bob", initialized, session_id)
        as_bob = await post_as(http, "token-bob", call, session_id)
        as_carol = await post_as(http, "token-carol", call, session_id)
        renewed = await post_as(http, "token-bob-renewed", call, session_id)

    assert opened.status_code == 200
    assert as_bob.status_code == 200
    assert as_bob.json()["result"]["structuredContent"] == LISTING.expected
    assert 400 <= as_carol.status_code < 500
    assert renewed.status_code == 200
    assert renewed.json()["result"]["structuredContent"] == LISTING.expected
    assert [call.path for call in recorded(api)] == [
        access_requests_path("bob"),
        access_requests_path("bob"),
    ]


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer token-mallory"}, {"Authorization": "Basic Ym9iOmJvYg=="}],
    ids=["no token", "unknown token", "not a bearer token"],
)
async def test_a_request_without_a_valid_token_is_refused(
    api: HTTPServer, settings: Settings, headers: dict[str, str]
) -> None:
    serve_listing(api, "bob")
    server = authenticated_server(settings, StubVerifier())

    async with http_to(server, headers) as http:
        response = await http.post(
            "/mcp",
            json=TOOL_CALL,
            headers=JSON_ACCEPT,
        )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Bearer ")
    assert "oauth-protected-resource" in response.headers["WWW-Authenticate"]
    assert api.log == []


async def test_a_token_without_a_subject_sends_nothing(api: HTTPServer, settings: Settings) -> None:
    serve_listing(api, "bob")
    server = authenticated_server(settings, StubVerifier())

    result = await list_as(server, "token-nobody")

    assert "The caller could not be identified" in error_text(result)
    assert api.log == []


async def test_an_explicit_identity_wins_over_the_token_subject(api: HTTPServer) -> None:
    settings = settings_for(base_url(api))
    serve_listing(api, "carol")
    server = create_server(
        settings, identity=bound_user("carol"), auth=AUTH, token_verifier=StubVerifier()
    )

    result = await list_as(server, "token-bob")

    assert payload(result) == LISTING.expected
    assert [call.path for call in recorded(api)] == [access_requests_path("carol")]


async def test_server_with_a_token_verifier_explains_the_token_subject(
    settings: Settings,
) -> None:
    server = authenticated_server(settings, StubVerifier())

    async with (
        http_to(server, {"Authorization": "Bearer token-bob"}) as http,
        Client(streamable_http_client(RESOURCE_URL, http_client=http)) as client,
    ):
        instructions = client.instructions

    assert instructions is not None
    assert (
        "act as the Permit user named by the subject of the caller's verified access token"
        in instructions
    )


def test_a_token_verifier_without_auth_settings_is_refused_by_mcp(settings: Settings) -> None:
    with pytest.raises(ValueError, match="token_verifier without auth settings"):
        create_server(settings, token_verifier=StubVerifier())


def test_auth_settings_without_a_token_verifier_are_refused_by_mcp(settings: Settings) -> None:
    with pytest.raises(
        ValueError, match="Must specify either auth_server_provider or token_verifier"
    ):
        create_server(settings, identity=bound_user("alice"), auth=AUTH)


def test_without_any_identity_the_error_names_every_way_to_set_one(
    settings: Settings,
) -> None:
    assert settings.user is None
    with pytest.raises(ConfigError) as caught:
        create_server(settings, auth=AUTH)

    assert str(caught.value) == (
        "Set PERMIT_MCP_USER to the Permit user key this server acts as: every tool call is "
        "made as that user. A host that identifies each caller passes identity= to "
        "create_server() instead, or token_verifier= and auth= to act as the subject of each "
        "caller's access token."
    )
