"""create_server, PermitTools.register and the tool metadata clients see."""

from __future__ import annotations

import inspect
from importlib.metadata import version
from typing import TYPE_CHECKING, Any

import pytest
from mcp.client import Client
from mcp.server.mcpserver import MCPServer

import permit_mcp
from permit_mcp import TOOL_NAMES, ConfigError, PermitTools, bound_user, create_server
from tests.support import (
    API_KEY,
    CASES,
    Call,
    access_requests_path,
    base_url,
    call_tool,
    connected,
    error_text,
    payload,
    recorded,
    serve_case,
    serve_json,
    server_of,
    settings_for,
)

if TYPE_CHECKING:
    from mcp.types import Tool
    from pytest_httpserver import HTTPServer

    from permit_mcp import Settings

ACCESS_REQUEST_TOOLS = {
    "create_access_request",
    "list_access_requests",
    "approve_access_request",
    "deny_access_request",
    "cancel_access_request",
}
OPERATION_APPROVAL_TOOLS = {
    "create_operation_approval",
    "list_operation_approvals",
    "approve_operation_approval",
    "deny_operation_approval",
    "cancel_operation_approval",
}
ALWAYS_REGISTERED = {"list_resource_instances", "check_permission"}
LIST_TOOLS = {
    "list_resource_instances",
    "list_access_requests",
    "list_operation_approvals",
}
READ_ONLY_TOOLS = LIST_TOOLS | {"check_permission"}
PUBLIC_NAMES = {
    "Settings",
    "ConfigError",
    "IdentityResolver",
    "IdentityError",
    "bound_user",
    "access_token_subject",
    "PermitTools",
    "TOOL_NAMES",
    "create_server",
    "__version__",
}


async def tools_of(server: MCPServer[Any]) -> dict[str, Tool]:
    async with Client(server) as client:
        return {tool.name: tool for tool in (await client.list_tools()).tools}


def test_public_api() -> None:
    assert set(permit_mcp.__all__) == PUBLIC_NAMES
    assert permit_mcp.__version__ == version("permit-mcp")


def test_create_server_without_any_identity_is_a_config_error(
    settings: Settings,
) -> None:
    assert settings.user is None
    with pytest.raises(ConfigError, match="PERMIT_MCP_USER"):
        create_server(settings)


def test_create_server_needs_no_running_event_loop(settings: Settings) -> None:
    server = create_server(settings, identity=bound_user("alice"))

    assert server.name == "permit"


async def test_settings_user_is_the_default_identity(api: HTTPServer) -> None:
    settings = settings_for(base_url(api), user="carol")
    path = access_requests_path("carol")
    serve_json(api, Call("GET", path), {"data": []})

    async with Client(create_server(settings)) as client:
        result = await client.call_tool("list_access_requests", {})

    assert payload(result) == {"data": []}
    assert [call.path for call in recorded(api)] == [path]


async def test_create_server_reads_the_environment(
    api: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PERMIT_API_KEY", API_KEY)
    monkeypatch.setenv("PERMIT_RESOURCE", "documents")
    monkeypatch.setenv("PERMIT_TENANT", "acme")
    monkeypatch.setenv("PERMIT_API_URL", base_url(api))
    monkeypatch.setenv("PERMIT_ACCESS_REQUEST_ELEMENT", "ar-elem")
    monkeypatch.setenv("PERMIT_MCP_USER", "alice")
    case = CASES["list_resource_instances"]
    serve_case(api, case)

    async with Client(create_server()) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        result = await client.call_tool("list_resource_instances", case.arguments)

    assert names == ALWAYS_REGISTERED | ACCESS_REQUEST_TOOLS
    assert payload(result) == case.expected
    assert recorded(api) == list(case.calls)


async def test_server_explains_the_bound_user(api: HTTPServer) -> None:
    server = create_server(settings_for(base_url(api), user="alice"))

    async with Client(server) as client:
        instructions = client.instructions

    assert instructions is not None
    assert "act as the Permit user 'alice'" in instructions
    assert "check_permission asks the PDP about that user" in instructions
    assert "list_resource_instances lists with the server's credentials" in instructions


async def test_server_with_a_resolver_explains_the_identified_caller(
    settings: Settings,
) -> None:
    async with connected(settings) as client:
        instructions = client.instructions

    assert instructions is not None
    assert "act as the Permit user this server identifies the caller as" in instructions


@pytest.mark.parametrize(
    ("access_request_element", "operation_approval_element", "expected"),
    [
        ("ar-elem", None, ALWAYS_REGISTERED | ACCESS_REQUEST_TOOLS),
        (None, "oa-elem", ALWAYS_REGISTERED | OPERATION_APPROVAL_TOOLS),
        ("ar-elem", "oa-elem", set(TOOL_NAMES)),
    ],
)
async def test_tools_need_their_element(
    api: HTTPServer,
    access_request_element: str | None,
    operation_approval_element: str | None,
    expected: set[str],
) -> None:
    settings = settings_for(
        base_url(api),
        access_request_element=access_request_element,
        operation_approval_element=operation_approval_element,
    )
    host: MCPServer[Any] = MCPServer(name="host")
    tools = PermitTools(settings, bound_user("alice"))
    try:
        registered = tools.register(host)
        listed = await tools_of(host)
    finally:
        await tools.aclose()

    assert set(registered) == expected
    assert len(registered) == len(expected)
    assert set(listed) == expected


async def test_register_excludes_named_tools(settings: Settings) -> None:
    host: MCPServer[Any] = MCPServer(name="host")
    tools = PermitTools(settings, bound_user("alice"))
    try:
        registered = tools.register(
            host, exclude=["deny_access_request", "deny_operation_approval"]
        )
        listed = await tools_of(host)
    finally:
        await tools.aclose()

    expected = set(TOOL_NAMES) - {"deny_access_request", "deny_operation_approval"}
    assert set(registered) == set(listed) == expected


async def test_register_keeps_the_host_tools(api: HTTPServer, settings: Settings) -> None:
    host: MCPServer[Any] = MCPServer(name="host")

    @host.tool()
    def ping() -> str:
        return "pong"

    case = CASES["list_resource_instances"]
    serve_case(api, case)
    tools = PermitTools(settings, bound_user("alice"))
    try:
        tools.register(host)
        async with Client(host) as client:
            listed = {tool.name for tool in (await client.list_tools()).tools}
            result = await client.call_tool("list_resource_instances", case.arguments)
    finally:
        await tools.aclose()

    assert listed == set(TOOL_NAMES) | {"ping"}
    assert payload(result) == case.expected


def test_register_rejects_unknown_exclusions(settings: Settings) -> None:
    host: MCPServer[Any] = MCPServer(name="host")
    tools = PermitTools(settings, bound_user("alice"))

    with pytest.raises(ValueError, match="list_resource_instances") as caught:
        tools.register(host, exclude=["delete_everything"])

    assert "delete_everything" in str(caught.value)


def test_create_server_rejects_unknown_exclusions(settings: Settings) -> None:
    with pytest.raises(ValueError, match="no_such_tool"):
        create_server(settings, identity=bound_user("alice"), exclude_tools=["no_such_tool"])


async def test_create_server_excludes_tools(settings: Settings) -> None:
    server = create_server(
        settings,
        identity=bound_user("alice"),
        exclude_tools=["list_resource_instances"],
    )

    assert set(await tools_of(server)) == set(TOOL_NAMES) - {"list_resource_instances"}


def undescribed(tools: list[Tool]) -> list[str]:
    """Name each tool, and each tool argument, whose description is missing or blank."""
    missing: list[str] = []
    for tool in tools:
        if not (tool.description or "").strip():
            missing.append(tool.name)
        for prop, schema in tool.input_schema.get("properties", {}).items():
            if not str(schema.get("description") or "").strip():
                missing.append(f"{tool.name}.{prop}")
    return missing


def undocumented(names: list[str]) -> list[str]:
    """Name each public function, class and public method of `names` without a docstring."""
    missing: list[str] = []
    for name in names:
        obj = getattr(permit_mcp, name)
        if not (inspect.isfunction(obj) or inspect.isclass(obj)):
            continue
        if not (obj.__doc__ or "").strip():
            missing.append(name)
        if not inspect.isclass(obj):
            continue
        for member_name, member in vars(obj).items():
            function = getattr(member, "__func__", member)
            # mutmut adds a class's mutants to it under non-ASCII names (xǁClassǁname__mutmut_1).
            if member_name.startswith("_") or not member_name.isascii():
                continue
            if not inspect.isfunction(function):
                continue
            if not (function.__doc__ or "").strip():
                missing.append(f"{name}.{member_name}")
    return missing


async def test_every_tool_and_argument_is_described(settings: Settings) -> None:
    async with connected(settings) as client:
        tools = (await client.list_tools()).tools

    assert len(tools) == len(TOOL_NAMES)
    assert undescribed(tools) == []


async def test_an_undescribed_tool_or_argument_is_found() -> None:
    host: MCPServer[Any] = MCPServer(name="host")

    def bare(count: int) -> int:
        return count

    host.add_tool(bare, name="bare", description=" ")

    assert undescribed(list((await tools_of(host)).values())) == ["bare", "bare.count"]


def test_every_public_function_class_and_method_has_a_docstring() -> None:
    assert undocumented(permit_mcp.__all__) == []


def test_an_undocumented_public_function_or_method_is_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Planted:
        """Documented."""

        def method(self) -> None:
            pass

        @classmethod
        def build(cls) -> None:
            pass

        def _private(self) -> None:
            pass

    def planted_function() -> None:
        pass

    monkeypatch.setattr(permit_mcp, "Planted", Planted, raising=False)
    monkeypatch.setattr(permit_mcp, "planted_function", planted_function, raising=False)

    assert undocumented(["Planted", "planted_function", "TOOL_NAMES"]) == [
        "Planted.method",
        "Planted.build",
        "planted_function",
    ]


async def test_list_tools_are_read_only(settings: Settings) -> None:
    async with connected(settings) as client:
        tools = (await client.list_tools()).tools

    for tool in tools:
        assert tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is (tool.name in READ_ONLY_TOOLS), tool.name


async def test_paging_bounds_are_in_the_schema(settings: Settings) -> None:
    async with connected(settings) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    for name in LIST_TOOLS:
        properties = tools[name].input_schema["properties"]
        assert properties["page"]["minimum"] == 1
        assert properties["per_page"]["minimum"] == 1
        assert properties["per_page"]["maximum"] == 100


async def test_status_choices_are_in_the_schema(settings: Settings) -> None:
    async with connected(settings) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    status = str(tools["list_access_requests"].input_schema["properties"]["status"])
    for choice in ("pending", "approved", "denied", "canceled"):
        assert choice in status


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("list_resource_instances", {"page": 0}),
        ("list_resource_instances", {"per_page": 0}),
        ("list_resource_instances", {"per_page": 101}),
        ("list_access_requests", {"status": "everything"}),
        ("list_operation_approvals", {"per_page": 1000}),
        ("create_access_request", {"reason": "missing role"}),
        ("approve_access_request", {}),
        ("cancel_access_request", {}),
        ("cancel_operation_approval", {}),
        ("check_permission", {}),
        ("check_permission", {"action": ""}),
        ("check_permission", {"action": "read", "resource_instance": ""}),
    ],
)
async def test_invalid_arguments_send_nothing(
    api: HTTPServer, pdp: HTTPServer, settings: Settings, name: str, arguments: dict[str, Any]
) -> None:
    case = CASES[name]
    serve_case(server_of(case, api, pdp), case)

    result = await call_tool(settings, name, arguments)

    error_text(result)
    assert api.log == []
    assert pdp.log == []
