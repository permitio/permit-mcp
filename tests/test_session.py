"""One HTTP connection pool per server, closed by aclose(), the lifespan, or at collection."""

from __future__ import annotations

import asyncio
import gc
import json
import threading
import warnings
import weakref
from typing import TYPE_CHECKING, Any

from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from werkzeug import Response

from permit_mcp import PermitTools, bound_user, create_server
from permit_mcp.permit_api import PermitApi
from tests.keepalive import KeepAliveServer
from tests.support import (
    FACTS,
    PDP_PATH,
    SCOPE,
    SCOPE_PATH,
    connected,
    payload,
    settings_for,
)

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


async def test_api_and_pdp_calls_share_one_session() -> None:
    routes = {**ROUTES, ("POST", PDP_PATH): {"allow": True}}
    async with KeepAliveServer(routes) as permit:
        settings = settings_for(permit.url, pdp_url=permit.url)
        async with connected(settings) as client:
            listed = await client.call_tool("list_resource_instances", {})
            checked = await client.call_tool("check_permission", {"action": "read"})
            again = await client.call_tool("list_resource_instances", {})

        assert payload(listed) == payload(again) == INSTANCES
        assert payload(checked) == {"allowed": True}
        assert permit.requests == [
            ("GET", SCOPE_PATH),
            ("GET", f"{FACTS}/resource_instances"),
            ("POST", PDP_PATH),
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


# --- an instance dropped without aclose() ---------------------------------------------------


def _messages(caught: list[warnings.WarningMessage]) -> list[str]:
    return [f"{w.category.__name__}: {w.message}" for w in caught]


async def test_dropping_an_unclosed_api_closes_its_connections_without_a_warning() -> None:
    loop = asyncio.get_running_loop()
    reported: list[str] = []
    loop.set_exception_handler(lambda _loop, context: reported.append(context["message"]))
    try:
        async with KeepAliveServer(ROUTES) as permit:
            api = PermitApi(settings_for(permit.url))
            await api.list_resource_instances(page=1, per_page=1)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                del api
                gc.collect()
            await permit.wait_until_all_closed()
    finally:
        loop.set_exception_handler(None)

    assert _messages(caught) == []
    assert reported == []
    assert permit.connections == permit.closed == 1


def test_dropping_an_unclosed_api_after_its_loop_closed_leaves_aiohttp_quiet() -> None:
    async def open_session() -> PermitApi:
        async with KeepAliveServer(ROUTES) as permit:
            api = PermitApi(settings_for(permit.url))
            await api.list_resource_instances(page=1, per_page=1)
            return api

    reported: list[str] = []
    with asyncio.Runner() as runner:
        runner.get_loop().set_exception_handler(
            lambda _loop, context: reported.append(context["message"])
        )
        api = runner.run(open_session())
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        del api
        gc.collect()

    assert reported == [], "no Unclosed client session or Unclosed connector"
    # Only asyncio's own reports of the socket it could not close once its loop was closed.
    asyncio_own = (
        "ResourceWarning: unclosed <socket.socket",
        "ResourceWarning: unclosed transport",
    )
    assert [m for m in _messages(caught) if not m.startswith(asyncio_own)] == []


async def test_aclose_leaves_nothing_for_garbage_collection_to_close() -> None:
    async with KeepAliveServer(ROUTES) as permit:
        api = PermitApi(settings_for(permit.url))
        await api.list_resource_instances(page=1, per_page=1)
        session = weakref.ref(api._current_session())  # noqa: SLF001

        await api.aclose()
        await permit.wait_until_all_closed()
        gc.collect()
        assert session() is None, "aclose() lets go of the session, though the api lives on"

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            del api
            gc.collect()

    assert _messages(caught) == []
    assert permit.connections == permit.closed == 1


async def test_a_session_reopened_after_aclose_is_closed_when_the_api_is_dropped() -> None:
    async with KeepAliveServer(ROUTES) as permit:
        api = PermitApi(settings_for(permit.url))
        await api.list_resource_instances(page=1, per_page=1)
        await api.aclose()
        await api.list_resource_instances(page=2, per_page=1)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            del api
            gc.collect()
        await permit.wait_until_all_closed()

    assert _messages(caught) == []
    assert permit.connections == permit.closed == 2


async def test_a_session_replaced_after_it_closed_is_let_go() -> None:
    async with KeepAliveServer(ROUTES) as permit:
        api = PermitApi(settings_for(permit.url))
        await api.list_resource_instances(page=1, per_page=1)
        first = api._current_session()  # noqa: SLF001
        await first.close()
        await api.list_resource_instances(page=2, per_page=1)
        replaced = weakref.ref(first)
        del first
        gc.collect()

        assert replaced() is None, "the closed session is not kept for the finalizer"
        await api.aclose()
