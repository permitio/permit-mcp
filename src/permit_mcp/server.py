"""Builds the Permit MCP server, and the `permit-mcp` command that runs it over stdio."""

import contextlib
import sys
from collections.abc import AsyncIterator, Collection
from importlib.metadata import version
from typing import Any

from mcp.server.mcpserver import MCPServer

from permit_mcp._log import configure_cli_logging, scrub
from permit_mcp.config import ENV_VARS, ConfigError, Settings
from permit_mcp.identity import IdentityResolver, bound_user
from permit_mcp.tools import PermitTools

SERVER_NAME = "permit"
CONFIG_ERROR_EXIT_CODE = 2


def create_server(
    settings: Settings | None = None,
    *,
    identity: IdentityResolver | None = None,
    exclude_tools: Collection[str] = (),
) -> MCPServer[Any]:
    """Build an MCP server with the Permit tools registered.

    Args:
        settings: The settings; read with `Settings.from_env()` when None.
        identity: Returns the Permit user key of each tool call's caller. When None, every
            call acts as `settings.user` (`PERMIT_MCP_USER`).
        exclude_tools: Names of tools to leave out.

    Returns:
        The server. The MCP SDK enters its lifespan once per connection for some transports
        (SSE, in-memory clients), so connections are counted, and the HTTP session to Permit
        is closed when the last open one ends. A call made while no session is open opens
        one.

    Raises:
        ConfigError: The settings are invalid, or neither `identity` nor `settings.user`
            says who the tools act as.
        ValueError: `exclude_tools` names a tool that does not exist.

    """
    if settings is None:
        settings = Settings.from_env()
    if identity is None:
        if settings.user is None:
            msg = (
                f"Set {ENV_VARS['user']} to the Permit user key this server acts as: every "
                "tool call is made as that user. A host that identifies each caller passes "
                "identity= to create_server() instead."
            )
            raise ConfigError(msg)
        identity = bound_user(settings.user)
        acting = f"the Permit user '{settings.user}'"
    else:
        acting = "the Permit user this server identifies the caller as"

    tools = PermitTools(settings, identity)
    open_lifespans = 0

    @contextlib.asynccontextmanager
    async def lifespan(_server: MCPServer[None]) -> AsyncIterator[None]:
        nonlocal open_lifespans
        open_lifespans += 1
        try:
            yield
        finally:
            open_lifespans -= 1
            if open_lifespans == 0:
                await tools.aclose()

    server: MCPServer[Any] = MCPServer(
        SERVER_NAME,
        instructions=(
            "Tools for Permit.io access requests, operation approvals and permission checks on "
            f"the '{settings.resource}' resource type in tenant '{settings.tenant}'. The "
            f"access-request and operation-approval tools act as {acting}, and "
            "check_permission asks the PDP about that user; no tool takes a "
            "user argument, and the acting user cannot be changed through tool arguments. "
            "list_resource_instances lists with the server's credentials, not filtered by the "
            "caller's permissions, and still requires an identified caller. Use it to find "
            "instance keys, and the list tools to find request IDs before approving or denying."
        ),
        version=version("permit-mcp"),
        lifespan=lifespan,
    )
    tools.register(server, exclude=exclude_tools)
    return server


def main() -> None:
    """Run the server over stdio: the `permit-mcp` command.

    Configuration comes from the environment (see `Settings.from_env`), and every tool call
    acts as `PERMIT_MCP_USER`. Log records go to stderr, as stdout carries the protocol. A
    configuration error is printed to stderr without a traceback and exits with status 2.
    """
    configure_cli_logging()
    try:
        server = create_server()
    except ConfigError as exc:
        sys.stderr.write(f"permit-mcp: configuration error: {scrub(str(exc))}\n")
        sys.exit(CONFIG_ERROR_EXIT_CODE)
    server.run("stdio")
