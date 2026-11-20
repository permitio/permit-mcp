"""Read-only schema tools and the per-call `env` / `tenant` override."""

import asyncio

import httpx
from mcp.server.fastmcp import FastMCP

from permit_mcp import server

RESOURCES = [
    {"key": "inv_einvoice", "name": "E-Invoice",
     "actions": {"list": {}, "get": {}}, "roles": {"member": {}}},
    {"key": "bkg_invoice", "name": "Invoice", "actions": {"get": {}}, "roles": {}},
]
ROLES = [
    {"key": "accountant", "name": "Accountant",
     "permissions": ["inv_einvoice:list", "bkg_invoice:get"]},
    {"key": "viewer", "name": "Viewer", "permissions": ["bkg_invoice:get"]},
]
RESOURCE_ROLES = [{"key": "member", "name": "Member", "permissions": ["get"]}]


def _tools(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        path = request.url.path
        if path.endswith("/resources/inv_einvoice/roles"):
            return httpx.Response(200, json=RESOURCE_ROLES)
        if path.endswith("/resources"):
            return httpx.Response(200, json=RESOURCES)
        if path.endswith("/roles"):
            return httpx.Response(200, json=ROLES)
        if path.endswith("/resource_instances"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"detail": path})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(server.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(server, "PERMIT_API_KEY", "permit_key_test")
    monkeypatch.setattr(server, "PROJECT_ID", "proj")
    monkeypatch.setattr(server, "ENV_ID", "testing")
    monkeypatch.setattr(server, "TENANT", "abc-logistics")
    monkeypatch.setattr(server, "RESOURCE_KEY", None)
    return server.PermitServer(FastMCP("test")), seen


def test_list_resources_defaults_to_configured_env_and_filters(monkeypatch):
    tools, seen = _tools(monkeypatch)
    result = asyncio.run(tools.list_resources(key_contains="inv_"))
    assert seen[-1].path == "/v2/schema/proj/testing/resources"
    assert result == [{"key": "inv_einvoice", "name": "E-Invoice",
                       "actions": ["get", "list"], "roles": ["member"]}]


def test_list_roles_by_resource_includes_rebac_roles_and_honours_env(monkeypatch):
    tools, seen = _tools(monkeypatch)
    result = asyncio.run(tools.list_roles(env="production", resource="inv_einvoice"))
    assert [u.path for u in seen] == [
        "/v2/schema/proj/production/roles",
        "/v2/schema/proj/production/resources/inv_einvoice/roles",
    ]
    assert result == [
        {"key": "accountant", "name": "Accountant", "scope": "top-level",
         "permissions": ["inv_einvoice:list"]},
        {"key": "member", "name": "Member", "scope": "inv_einvoice", "permissions": ["get"]},
    ]


def test_list_resource_instances_env_and_tenant_override(monkeypatch):
    tools, seen = _tools(monkeypatch)
    asyncio.run(tools.list_resource_instances(env="dev", tenant="abex-logistics"))
    assert seen[-1].path == "/v2/facts/proj/dev/resource_instances"
    assert seen[-1].params["tenant"] == "abex-logistics"
    asyncio.run(tools.list_resource_instances())
    assert seen[-1].path == "/v2/facts/proj/testing/resource_instances"
    assert seen[-1].params["tenant"] == "abc-logistics"
