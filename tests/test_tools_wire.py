"""Every tool, driven through the MCP protocol, against a local mock of the Permit API.

Each case pins the exact requests a tool sends (method, raw path, query, JSON body,
Authorization) and the value the MCP client receives back.
"""

from __future__ import annotations

import socket
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from permit_mcp import TOOL_NAMES, bound_user
from permit_mcp.permit_api import PermitApi, PermitApiError
from tests.support import (
    API_KEY,
    APPROVE_DENY_TOOLS,
    AR_ELEMENTS_PATH,
    AR_ID,
    AR_PATH,
    BY_ID_TOOLS,
    CASES,
    ELEMENT_AUTH,
    ELEMENT_TOKEN,
    ELEMENTS_TOKEN_TOOLS,
    LOGIN_PATH,
    LOGIN_RESPONSE,
    OA_ID,
    OA_PATH,
    PDP_PATH,
    REDIRECT_TOKEN,
    REQUESTER,
    RESOURCE,
    SCOPE_PATH,
    TENANT,
    USER,
    Call,
    access_requests_path,
    base_url,
    call_tool,
    connected,
    error_text,
    login_call,
    payload,
    recorded,
    recorded_requests,
    serve_case,
    serve_data,
    serve_json,
    server_of,
    settings_for,
)

if TYPE_CHECKING:
    from pytest_httpserver import HTTPServer

    from permit_mcp import Settings

LIST_TOOLS = ["list_access_requests", "list_operation_approvals"]


async def test_every_tool_has_a_case(settings: Settings) -> None:
    async with connected(settings) as client:
        registered = {tool.name for tool in (await client.list_tools()).tools}
    assert registered == set(CASES) == set(TOOL_NAMES)
    assert len(TOOL_NAMES) == len(set(TOOL_NAMES)) == 12


@pytest.mark.parametrize("name", sorted(CASES))
async def test_tool_sends_exact_request_and_returns_api_json(
    api: HTTPServer, pdp: HTTPServer, settings: Settings, name: str
) -> None:
    case = CASES[name]
    target, other = (pdp, api) if case.pdp else (api, pdp)
    serve_case(target, case)

    result = await call_tool(settings, name, case.arguments)

    assert payload(result) == case.expected
    assert recorded(target) == list(case.calls)
    assert other.log == []
    for request in recorded_requests(target):
        if request.get_data():
            assert request.mimetype == "application/json"


@pytest.mark.parametrize("name", sorted(CASES))
async def test_api_error_reports_status_without_the_api_key(
    api: HTTPServer, pdp: HTTPServer, settings: Settings, name: str
) -> None:
    case = CASES[name]
    target = server_of(case, api, pdp)
    serve_case(target, case, status=403, response={"detail": f"key {API_KEY} may not do this"})

    result = await call_tool(settings, name, case.arguments)

    text = error_text(result)
    assert "403" in text
    assert API_KEY not in text
    assert recorded(target) == list(case.calls)


@pytest.mark.parametrize("name", ELEMENTS_TOKEN_TOOLS)
async def test_elements_call_logs_in_as_the_bound_user_first(
    api: HTTPServer, settings: Settings, name: str
) -> None:
    case = CASES[name]
    serve_case(api, case)

    await call_tool(settings, name, case.arguments)

    login, elements = recorded(api)
    assert login == Call("POST", LOGIN_PATH, body={"user_id": "alice", "tenant_id": TENANT})
    assert login.authorization == f"Bearer {API_KEY}"
    assert elements.path.startswith(AR_ELEMENTS_PATH if "access_request" in name else OA_PATH)
    assert elements.authorization == f"Bearer {ELEMENT_TOKEN}"
    assert REDIRECT_TOKEN not in elements.authorization


@pytest.mark.parametrize(
    "login_response",
    [
        pytest.param(
            {**LOGIN_RESPONSE, "element_bearer_token": None, "error": "no such user"},
            id="error-field-set",
        ),
        pytest.param(
            {key: value for key, value in LOGIN_RESPONSE.items() if key != "element_bearer_token"},
            id="redirect-token-only",
        ),
        pytest.param(
            {**LOGIN_RESPONSE, "error": "tenant not found", "error_code": 404},
            id="error-with-token",
        ),
    ],
)
async def test_unusable_login_response_fails_before_the_elements_call(
    api: HTTPServer, settings: Settings, login_response: dict[str, Any]
) -> None:
    case = CASES["list_operation_approvals"]
    serve_json(api, login_call(), login_response)
    serve_json(api, case.calls[-1], case.response)

    result = await call_tool(settings, "list_operation_approvals", case.arguments)

    error_text(result)
    assert recorded(api) == [login_call()]


async def test_login_http_error_fails_before_the_elements_call(
    api: HTTPServer, settings: Settings
) -> None:
    case = CASES["create_operation_approval"]
    serve_json(api, login_call(), {"detail": f"bad key {API_KEY}"}, status=401)
    serve_json(api, case.calls[-1], case.response)

    result = await call_tool(settings, "create_operation_approval", case.arguments)

    text = error_text(result)
    assert "401" in text
    assert API_KEY not in text
    assert recorded(api) == [login_call()]


@pytest.mark.parametrize("name", LIST_TOOLS)
@pytest.mark.parametrize("filtered", [True, False], ids=["filters", "no-filters"])
async def test_list_query_carries_no_resource(
    api: HTTPServer, settings: Settings, name: str, *, filtered: bool
) -> None:
    # The element scopes a listing. For an RBAC element, Permit matches a resource filter
    # against a value it never stores, and lists nothing.
    case = CASES[name]
    serve_case(api, case)

    result = await call_tool(settings, name, case.arguments if filtered else {})

    payload(result)
    listing = recorded(api)[-1]
    assert listing.path == case.calls[-1].path
    assert "resource" not in listing.query
    assert RESOURCE not in listing.query.values()


@pytest.mark.parametrize("name", LIST_TOOLS)
async def test_list_returns_requesters_as_permit_sends_them(
    api: HTTPServer, settings: Settings, name: str
) -> None:
    case = CASES[name]
    items = [
        {"id": AR_ID, "status": "pending", **REQUESTER},
        {"id": OA_ID, "status": "approved", "requesting_user_id": "gone"},
        {"id": "no-requester", "status": "pending"},
    ]
    listing = {"data": items, "total_count": 3, "page_count": 1, "extra": {"kept": True}}
    serve_case(api, case, response=listing)

    result = await call_tool(settings, name, case.arguments)

    assert payload(result) == listing
    assert recorded(api) == list(case.calls)
    assert not [call for call in recorded(api) if "/users" in call.path]


async def test_list_access_requests_omits_unset_filters(
    api: HTTPServer, settings: Settings
) -> None:
    listing = Call("GET", AR_PATH, query={"page": "1", "per_page": "30"})
    serve_json(api, listing, {"data": [], "total_count": 0, "page_count": 0})

    result = await call_tool(settings, "list_access_requests", {})

    assert payload(result) == {"data": [], "total_count": 0, "page_count": 0}
    assert recorded(api) == [listing]


async def test_list_operation_approvals_omits_unset_filters(
    api: HTTPServer, settings: Settings
) -> None:
    listing = Call(
        "GET",
        OA_PATH,
        query={"page": "1", "per_page": "30"},
        authorization=ELEMENT_AUTH,
    )
    serve_json(api, login_call(), LOGIN_RESPONSE)
    serve_json(api, listing, {"data": [], "total_count": 0, "page_count": 0})

    result = await call_tool(settings, "list_operation_approvals", {})

    assert payload(result) == {"data": [], "total_count": 0, "page_count": 0}
    assert recorded(api) == [login_call(), listing]


@pytest.mark.parametrize("name", ["create_access_request", "create_operation_approval"])
async def test_create_without_instance_leaves_it_out_of_the_body(
    api: HTTPServer, settings: Settings, name: str
) -> None:
    case = CASES[name]
    arguments = {key: value for key, value in case.arguments.items() if key != "resource_instance"}
    *before, last = case.calls
    body = dict(last.body)
    details = {k: v for k, v in body["access_request_details"].items() if k != "resource_instance"}
    body["access_request_details"] = details
    case = replace(case, arguments=arguments, calls=(*before, replace(last, body=body)))
    serve_case(api, case)

    result = await call_tool(settings, name, case.arguments)

    assert payload(result) == case.expected
    assert recorded(api) == list(case.calls)


@pytest.mark.parametrize("name", APPROVE_DENY_TOOLS)
async def test_review_without_comment_sends_an_empty_object(
    api: HTTPServer, settings: Settings, name: str
) -> None:
    case = CASES[name]
    arguments = {key: value for key, value in case.arguments.items() if key != "reviewer_comment"}
    *before, last = case.calls
    case = replace(case, arguments=arguments, calls=(*before, replace(last, body={})))
    serve_case(api, case)

    result = await call_tool(settings, name, case.arguments)

    assert payload(result) == case.expected
    assert recorded(api) == list(case.calls)


@pytest.mark.parametrize("name", BY_ID_TOOLS)
async def test_change_with_empty_response_returns_null(
    api: HTTPServer, settings: Settings, name: str
) -> None:
    case = CASES[name]
    *before, last = case.calls
    for call in before:
        serve_json(api, call, LOGIN_RESPONSE)
    serve_data(api, last, "", status=204)

    result = await call_tool(settings, name, case.arguments)

    record_key = "operation_approval" if "operation_approval" in name else "access_request"
    assert payload(result) == {"status": case.expected["status"], record_key: None}


async def test_path_segments_are_escaped(api: HTTPServer, settings: Settings) -> None:
    user = "carol ops+team?#"
    path = f"{access_requests_path(user)}/{AR_ID}/approve"
    assert "carol%20ops%2Bteam%3F%23" in path
    call = Call("PUT", path, body={})
    serve_json(api, call, {"id": AR_ID, "status": "approved"})

    result = await call_tool(
        settings, "approve_access_request", {"access_request_id": AR_ID}, bound_user(user)
    )

    assert payload(result) == {
        "status": "approved",
        "access_request": {"id": AR_ID, "status": "approved"},
    }
    assert recorded(api) == [call]


@pytest.mark.parametrize("user", [".", "..", "a/b", "a\\b"])
async def test_user_key_that_would_change_the_path_sends_nothing(
    api: HTTPServer, settings: Settings, user: str
) -> None:
    serve_case(api, CASES["list_access_requests"])

    result = await call_tool(settings, "list_access_requests", {}, bound_user(user))

    assert "request path" in error_text(result)
    assert api.log == []


@pytest.mark.parametrize("tenant", ["..", "a/b", "a\\b"])
@pytest.mark.parametrize("name", ["create_access_request", "approve_access_request"])
async def test_tenant_that_would_change_the_path_sends_nothing(
    api: HTTPServer, settings: Settings, tenant: str, name: str
) -> None:
    case = CASES[name]
    serve_case(api, case)

    result = await call_tool(replace(settings, tenant=tenant), name, case.arguments)

    assert "request path" in error_text(result)
    assert api.log == []


@pytest.mark.parametrize("element", ["..", "a/b", "a\\b"])
@pytest.mark.parametrize(
    "name", sorted(set(CASES) - {"list_resource_instances", "check_permission"})
)
async def test_element_that_would_change_the_path_sends_nothing(
    api: HTTPServer, settings: Settings, element: str, name: str
) -> None:
    case = CASES[name]
    serve_case(api, case)
    broken = replace(settings, access_request_element=element, operation_approval_element=element)

    result = await call_tool(broken, name, case.arguments)

    assert "request path" in error_text(result)
    assert api.log == []


@pytest.mark.parametrize("name", BY_ID_TOOLS)
@pytest.mark.parametrize("bad_id", ["ar-1", "..", "a/b", f"{AR_ID}/../x", ""])
async def test_non_uuid_id_sends_nothing(
    api: HTTPServer, settings: Settings, name: str, bad_id: str
) -> None:
    case = CASES[name]
    serve_case(api, case)
    id_argument = next(key for key in case.arguments if key.endswith("_id"))

    result = await call_tool(settings, name, {**case.arguments, id_argument: bad_id})

    error_text(result)
    assert api.log == []


async def test_ids_are_uuids_in_the_schema(settings: Settings) -> None:
    async with connected(settings) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    for name in BY_ID_TOOLS:
        properties = tools[name].input_schema["properties"]
        id_argument = next(key for key in properties if key.endswith("_id"))
        assert properties[id_argument]["format"] == "uuid", name


async def test_unset_element_fails_before_any_request(api: HTTPServer) -> None:
    permit = PermitApi(settings_for(base_url(api), access_request_element=None))
    try:
        with pytest.raises(PermitApiError, match="PERMIT_ACCESS_REQUEST_ELEMENT is not set"):
            await permit.list_access_requests(
                USER, status=None, role=None, resource_instance=None, page=1, per_page=30
            )
    finally:
        await permit.aclose()

    assert api.log == []


async def test_key_scope_is_looked_up_once_per_server(api: HTTPServer, settings: Settings) -> None:
    case = CASES["list_resource_instances"]
    serve_case(api, case)

    async with connected(settings) as client:
        first = await client.call_tool("list_resource_instances", case.arguments)
        second = await client.call_tool("list_resource_instances", case.arguments)

    assert payload(first) == payload(second) == case.expected
    scope_lookups = [r for r in recorded_requests(api) if r.path == SCOPE_PATH]
    assert len(scope_lookups) == 1
    assert scope_lookups[0].method == "GET"
    assert scope_lookups[0].headers.get("Authorization") == f"Bearer {API_KEY}"


@pytest.mark.parametrize(
    ("scope", "level"),
    [
        ({"organization_id": "org", "project_id": "proj-id", "environment_id": None}, "project"),
        ({"organization_id": "org", "project_id": None, "environment_id": None}, "organization"),
    ],
)
async def test_key_without_an_environment_is_rejected(
    api: HTTPServer, settings: Settings, scope: dict[str, Any], level: str
) -> None:
    api.clear()
    api.expect_request(SCOPE_PATH, method="GET").respond_with_json(scope)

    result = await call_tool(settings, "list_resource_instances", {})

    text = error_text(result)
    assert f"{level}-level API key" in text
    assert "needs an environment-level API key" in text
    assert recorded(api) == []


async def test_scope_response_that_is_not_an_object_is_an_error(
    api: HTTPServer, settings: Settings
) -> None:
    api.clear()
    api.expect_request(SCOPE_PATH, method="GET").respond_with_json(["proj-id", "env-id"])

    result = await call_tool(settings, "list_resource_instances", {})

    assert "not a JSON object" in error_text(result)
    assert recorded(api) == []


async def test_scope_lookup_error_hides_the_key(api: HTTPServer, settings: Settings) -> None:
    api.clear()
    api.expect_request(SCOPE_PATH, method="GET").respond_with_json(
        {"detail": f"invalid key {API_KEY}"}, status=401
    )

    result = await call_tool(settings, "list_resource_instances", {})

    text = error_text(result)
    assert "401" in text
    assert API_KEY not in text
    assert recorded(api) == []


async def test_non_json_response_is_a_tool_error(api: HTTPServer, settings: Settings) -> None:
    case = CASES["list_resource_instances"]
    serve_data(api, case.calls[-1], "<html>gateway</html>", status=200)

    result = await call_tool(settings, "list_resource_instances", case.arguments)

    text = error_text(result)
    assert "the response is not JSON" in text
    assert API_KEY not in text


async def test_error_body_is_redacted_before_it_is_cut(api: HTTPServer, settings: Settings) -> None:
    case = CASES["list_resource_instances"]
    padding = "x" * 1990
    serve_data(api, case.calls[-1], f"{padding}{API_KEY} trailing", status=500)

    result = await call_tool(settings, "list_resource_instances", case.arguments)

    text = error_text(result)
    assert text.endswith(f"{padding}[REDACTED]")
    assert API_KEY[:10] not in text


async def test_login_response_that_is_not_an_object_fails_before_the_elements_call(
    api: HTTPServer, settings: Settings
) -> None:
    case = CASES["create_operation_approval"]
    serve_json(api, login_call(), ["not", "an", "object"])
    serve_json(api, case.calls[-1], case.response)

    result = await call_tool(settings, "create_operation_approval", case.arguments)

    assert "not a JSON object" in error_text(result)
    assert recorded(api) == [login_call()]


@pytest.mark.parametrize("name", LIST_TOOLS)
@pytest.mark.parametrize(
    "listing", [{"items": []}, {"data": {"id": "x"}}, []], ids=["no-data", "data-object", "list"]
)
async def test_listing_without_a_data_list_is_an_error(
    api: HTTPServer, settings: Settings, name: str, listing: object
) -> None:
    serve_case(api, CASES[name], response=listing)

    result = await call_tool(settings, name, {})

    operation = name.replace("_", " ")
    assert (
        f"Permit API call to {operation} returned an unexpected shape: expected an object "
        "with a data list."
    ) in error_text(result)


async def test_unreachable_api_names_the_host() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    settings = settings_for(f"http://127.0.0.1:{port}")

    result = await call_tool(settings, "list_resource_instances", {})

    text = error_text(result)
    assert "127.0.0.1" in text
    assert API_KEY not in text


async def test_check_permission_without_an_instance_checks_the_type(
    api: HTTPServer, pdp: HTTPServer, settings: Settings
) -> None:
    check = Call(
        "POST",
        PDP_PATH,
        body={
            "user": {"key": USER},
            "action": "read",
            "resource": {"type": RESOURCE, "tenant": TENANT},
            "context": {},
        },
    )
    serve_json(pdp, check, {"allow": False, "result": False})

    result = await call_tool(settings, "check_permission", {"action": "read"})

    assert payload(result) == {"allowed": False}
    assert recorded(pdp) == [check]
    assert api.log == []


@pytest.mark.parametrize(
    "answer",
    [{}, {"result": True}, {"allow": "true"}, {"allow": 1}, {"allow": None}, [True], ""],
    ids=["empty", "result-only", "string", "number", "null", "list", "no-body"],
)
async def test_check_permission_answer_without_a_boolean_allow_is_an_error(
    pdp: HTTPServer, settings: Settings, answer: object
) -> None:
    case = CASES["check_permission"]
    if answer == "":
        serve_data(pdp, case.calls[-1], "", status=200)
    else:
        serve_json(pdp, case.calls[-1], answer)

    result = await call_tool(settings, "check_permission", case.arguments)

    assert "without a boolean allow" in error_text(result)


async def test_check_permission_404_says_which_pdp_to_set(
    pdp: HTTPServer, settings: Settings
) -> None:
    case = CASES["check_permission"]
    serve_case(pdp, case, status=404, response={"detail": f"no route, key {API_KEY}"})

    result = await call_tool(settings, "check_permission", case.arguments)

    text = error_text(result)
    assert "PDP call to check permission returned HTTP 404: " in text
    hint = "The URL in PERMIT_PDP_URL does not answer permission checks"
    assert text.index(hint) < text.index("The PDP answered: ")
    assert "no route, key [REDACTED]" in text
    assert API_KEY not in text


@pytest.mark.parametrize("status", [400, 401, 403, 500, 501])
async def test_check_permission_other_errors_carry_no_hint(
    pdp: HTTPServer, settings: Settings, status: int
) -> None:
    case = CASES["check_permission"]
    serve_case(pdp, case, status=status, response={"detail": "boom"})

    result = await call_tool(settings, "check_permission", case.arguments)

    text = error_text(result)
    assert f"PDP call to check permission returned HTTP {status}: " in text
    assert "Permit API" not in text
    assert "PERMIT_PDP_URL" not in text


async def test_api_errors_name_the_permit_api(api: HTTPServer, settings: Settings) -> None:
    case = CASES["cancel_access_request"]
    serve_case(api, case, status=403, response={"detail": "no"})

    result = await call_tool(settings, "cancel_access_request", case.arguments)

    assert "Permit API call to cancel access request returned HTTP 403: " in error_text(result)
