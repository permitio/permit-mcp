"""One HTTP connection pool per server, closed by aclose() or the server lifespan."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import TYPE_CHECKING, Any

from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from werkzeug import Response

from permit_mcp import PermitTools, bound_user, create_server
from tests.keepalive import KeepAliveServer
from tests.support import FACTS, SCOPE, SCOPE_PATH, connected, payload, settings_for

if TYPE_CHECKING:
    from pytest_httpserver import HTTPServer
    from werkzeug import Request

    from permit_mcp import Settings

INSTANCES = {"data": [{"key": "doc-1"}], "total_count": 1}
ROUTES = {
    ("GET", SCOPE_PATH): SCOPE,
    ("GET", f"{FACTS}/resource_instances"): INSTANCES,
}


async def test_tool_calls_share_one_connection() -> None:
    async with KeepAliveServer(ROUTES) as permit:
        async with connected(settings_for(permit.url)) as client:
            first = await client.call_tool("list_resource_instances", {})
            second = await client.call_tool("list_resource_instances", {"page": 2})
            third = await client.call_tool("list_resource_instances", {"page": 3})

        assert payload(first) == payload(second) == payload(third) == INSTANCES
        assert permit.requests == [
            ("GET", SCOPE_PATH),
            ("GET", f"{FACTS}/resource_instances"),
            ("GET", f"{FACTS}/resource_instances"),
            ("GET", f"{FACTS}/resource_instances"),
        ]
        assert permit.connections == 1


async def test_separate_clients_of_one_host_share_the_pool() -> None:
    async with KeepAliveServer(ROUTES) as permit:
        host: MCPServer[Any] = MCPServer(name="host")
        tools = PermitTools(settings_for(permit.url), bound_user("alice"))
        tools.register(host)
        try:
            for _ in range(2):
                async with Client(host) as client:
                    assert payload(await client.call_tool("list_resource_instances", {}))
        finally:
            await tools.aclose()

        assert permit.connections == 1


async def test_aclose_before_use_is_harmless(settings: Settings) -> None:
    tools = PermitTools(settings, bound_user("alice"))

    await tools.aclose()
    await tools.aclose()


async def test_aclose_closes_the_connections_and_is_idempotent() -> None:
    async with KeepAliveServer(ROUTES) as permit:
        host: MCPServer[Any] = MCPServer(name="host")
        tools = PermitTools(settings_for(permit.url), bound_user("alice"))
        tools.register(host)
        async with Client(host) as client:
            assert payload(await client.call_tool("list_resource_instances", {})) == INSTANCES

        await tools.aclose()
        await permit.wait_until_all_closed()
        await tools.aclose()

        assert permit.connections == permit.closed == 1


async def test_server_lifespan_closes_the_connections() -> None:
    async with KeepAliveServer(ROUTES) as permit:
        async with connected(settings_for(permit.url)) as client:
            assert payload(await client.call_tool("list_resource_instances", {})) == INSTANCES

        await permit.wait_until_all_closed()

        assert permit.connections == permit.closed == 1


async def test_a_client_disconnecting_does_not_cut_off_another(
    api: HTTPServer, settings: Settings
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def slow(_request: Request) -> Response:
        entered.set()
        release.wait(10)
        return Response(json.dumps(INSTANCES), mimetype="application/json")

    api.expect_request(f"{FACTS}/resource_instances").respond_with_handler(slow)
    server = create_server(settings, identity=bound_user("alice"))

    async with Client(server) as staying:
        call = asyncio.create_task(staying.call_tool("list_resource_instances", {}))
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            async with Client(server) as leaving:
                await leaving.list_tools()
        finally:
            release.set()
        result = await call

    assert payload(result) == INSTANCES
