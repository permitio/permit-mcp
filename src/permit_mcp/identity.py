"""Who a tool call acts as: identity resolvers, bound in code by whoever builds the server.

No tool takes a user argument. Every call asks the server's resolver for the Permit user
key of the caller, and a call whose caller cannot be identified fails before anything is
sent to Permit.
"""

from typing import Any, Protocol

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import Context


class IdentityError(Exception):
    """The caller of a tool could not be identified."""


class IdentityResolver(Protocol):
    """Returns the Permit user key of the caller of a tool call.

    Raise `IdentityError` when the caller cannot be identified. Any exception, or a
    return value that is not a non-empty string, fails the call without a Permit request.
    """

    async def __call__(self, ctx: Context[Any, Any], /) -> str:
        """Return the Permit user key of the caller of the tool call that `ctx` belongs to."""
        ...


def bound_user(user_key: str) -> IdentityResolver:
    """Return a resolver that acts as one fixed Permit user for every call.

    For a server that one person runs for themselves, such as the `permit-mcp` command over
    stdio: whoever can talk to the process is that person.

    Args:
        user_key: Key of the Permit user every tool call acts as.

    Returns:
        The resolver.

    Raises:
        ValueError: `user_key` is empty or only whitespace.

    """
    if not user_key.strip():
        msg = "bound_user() needs a non-empty Permit user key."
        raise ValueError(msg)

    async def resolve(_ctx: Context[Any, Any], /) -> str:
        return user_key

    return resolve


def access_token_subject() -> IdentityResolver:
    """Return a resolver that acts as the subject of the request's verified access token.

    For a server run over HTTP with the MCP SDK's authentication (a `TokenVerifier` and
    `AuthSettings`): the verifier's `AccessToken.subject` must be the caller's Permit user key.
    `create_server()` uses it when given a `token_verifier` and no `identity`.

    Returns:
        The resolver. It raises `IdentityError` when the request has no verified access
        token, or the token has no subject.

    """

    async def resolve(_ctx: Context[Any, Any], /) -> str:
        token = get_access_token()
        if token is None:
            msg = "the request carries no verified access token"
            raise IdentityError(msg)
        if not token.subject:
            msg = "the access token has no subject to use as the Permit user key"
            raise IdentityError(msg)
        return token.subject

    return resolve
