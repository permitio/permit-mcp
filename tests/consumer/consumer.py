"""A host application's use of permit-mcp, type-checked as a consumer sees the package.

`.github/scripts/check-consumer-types.sh` runs mypy (strict) on this file alone, against the
wheel installed in a fresh environment, never against `src/`. Nothing runs it, and pytest does
not collect it. It uses every name in `permit_mcp.__all__` as a host would
(`tests/test_consumer_fixture.py` checks that), so an API change that breaks a host fails the
check.

Each line marked `# type: ignore[<code>]` is a misuse that must stay an error with exactly that
code. The check runs with `warn_unused_ignores`, so a misuse that starts to type-check fails
it too. When an API change is intended, update this file to match (CONTRIBUTING.md).
"""

import contextlib
import os
import sys
from collections.abc import AsyncIterator
from typing import Any, assert_type

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context, MCPServer
from pydantic import AnyHttpUrl

import permit_mcp
from permit_mcp import (
    TOOL_NAMES,
    ConfigError,
    IdentityError,
    IdentityResolver,
    PermitTools,
    Settings,
    __version__,
    access_token_subject,
    bound_user,
    create_server,
)

USER_HEADER = "x-permit-user"
USER_AGENT = f"my-host permit-mcp/{__version__}"


class SharedTokenVerifier(TokenVerifier):
    """Accepts one bearer token, as the Permit user it was issued to."""

    def __init__(self, token: str, user_key: str) -> None:
        self._token = token
        self._user_key = user_key

    async def verify_token(self, token: str) -> AccessToken | None:
        if token != self._token:
            return None
        return AccessToken(token=token, client_id="my-host", scopes=[], subject=self._user_key)


async def header_user(ctx: Context[Any, Any], /) -> str:
    """Return the Permit user key a trusted proxy put in the request's headers."""
    user_key = (ctx.headers or {}).get(USER_HEADER)
    if not user_key:
        msg = f"the request has no {USER_HEADER} header"
        raise IdentityError(msg)
    return user_key


RESOLVERS: list[IdentityResolver] = [header_user, bound_user("alice"), access_token_subject()]


async def caller_or_none(resolver: IdentityResolver, ctx: Context[Any, Any]) -> str | None:
    try:
        return await resolver(ctx)
    except IdentityError:
        return None


def settings_from_env() -> Settings:
    try:
        settings = Settings.from_env(resource="documents", tenant=None)
    except ConfigError as exc:
        sys.exit(f"permit-mcp is not configured: {exc}")
    assert_type(settings.access_request_element, str | None)
    return settings


def settings_in_code() -> Settings:
    return Settings(
        api_key=os.environ["PERMIT_API_KEY"],
        resource="documents",
        tenant="default",
        api_url="https://api.permit.io",
        pdp_url="http://localhost:7766",
        access_request_element="document-access",
        operation_approval_element=None,
        user="alice",
    )


def stdio_server() -> MCPServer[Any]:
    """Acts as `settings.user` (PERMIT_MCP_USER) for every call."""
    server = create_server(settings_from_env(), exclude_tools={"approve_access_request"})
    assert_type(server, MCPServer[Any])
    return server


def resolver_server(settings: Settings) -> MCPServer[Any]:
    return create_server(settings, identity=header_user, exclude_tools=TOOL_NAMES[:1])


def http_server(settings: Settings) -> MCPServer[Any]:
    return create_server(
        settings,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl("https://auth.example.com"),
            resource_server_url=AnyHttpUrl("https://mcp.example.com/mcp"),
        ),
        token_verifier=SharedTokenVerifier("token", "alice"),
    )


def own_server(settings: Settings) -> tuple[MCPServer[None], list[str]]:
    tools = PermitTools(settings, access_token_subject())

    @contextlib.asynccontextmanager
    async def lifespan(_server: MCPServer[None]) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await tools.aclose()

    server = MCPServer("my-host", lifespan=lifespan)
    denials = [name for name in TOOL_NAMES if name.startswith("deny_")]
    registered = tools.register(server, exclude=denials)
    assert_type(registered, list[str])
    return server, registered


async def returns_int(_ctx: Context[Any, Any], /) -> int:
    return 1


def not_async(_ctx: Context[Any, Any], /) -> str:
    return "alice"


def misuses(settings: Settings, server: MCPServer[Any], tools: PermitTools) -> None:
    """Lines that must stay errors, each with the code its ignore names."""
    from permit_mcp import PermitServer  # type: ignore[attr-defined]  # noqa: PLC0415

    create_server(settings, identity=returns_int)  # type: ignore[arg-type]
    create_server(settings, identity=not_async)  # type: ignore[arg-type]
    create_server(settings, bound_user("alice"))  # type: ignore[call-arg]
    tools.register(server, exclude=1)  # type: ignore[arg-type]
    tools.register(server, ["deny_access_request"])  # type: ignore[call-arg]
    Settings(api_key=None, resource="documents")  # type: ignore[arg-type]
    Settings("api-key", "documents")  # type: ignore[call-arg]
    Settings.from_env(resource=1)  # type: ignore[arg-type]
    settings.api_key = "another-key"  # type: ignore[misc]
    permit_mcp.TOOL_NAMES = ["check_permission"]  # type: ignore[assignment]
    PermitServer(settings)
