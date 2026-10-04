"""One MCP server per chat session, bound to the user the backend authenticated.

The backend verifies the user's JWT when the chat's websocket opens, then builds a server
whose tools all act as that user: `bound_user(user)` is the identity resolver of the Permit
tools and of the app's own tools. The model talks to the server through an in-process MCP
client. Nothing the model sends, in a message or a tool argument, can change the user.
"""

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from permit import Permit

from food_ordering.db import Database, User
from food_ordering.tools import LIST_DISHES, ORDER_DISH, FoodTools
from permit_mcp import TOOL_NAMES, PermitTools, Settings, bound_user

# What a child's session offers. Permit enforces who may review requests either way; leaving
# the reviewer tools out keeps the model from offering what a child cannot do.
CHILD_TOOLS = frozenset(
    {
        "list_resource_instances",
        "create_access_request",
        "create_operation_approval",
        LIST_DISHES,
        ORDER_DISH,
    }
)


def build_server(
    settings: Settings, db: Database, permit: Permit, user: User
) -> tuple[MCPServer[Any], PermitTools]:
    """Build a server whose tools act as `user`.

    Returns:
        The server, and its Permit tools, whose HTTP session the caller closes with
        `aclose()` when the session ends.

    """
    identity = bound_user(user.username)
    server: MCPServer[Any] = MCPServer(
        "food-ordering",
        instructions=(
            "Tools of a family food-ordering app. Every tool acts as the signed-in user; no "
            "tool takes a user argument."
        ),
    )
    permit_tools = PermitTools(settings, identity)
    excluded = set() if user.role == "parent" else set(TOOL_NAMES) - CHILD_TOOLS
    permit_tools.register(server, exclude=excluded)
    FoodTools(db, permit, settings, identity).register(server)
    return server, permit_tools


@contextlib.asynccontextmanager
async def connect(
    settings: Settings, db: Database, permit: Permit, user: User
) -> AsyncIterator[Client]:
    """Yield an in-process MCP client connected to a server bound to `user`."""
    server, permit_tools = build_server(settings, db, permit, user)
    try:
        async with Client(server) as client:
            yield client
    finally:
        await permit_tools.aclose()
