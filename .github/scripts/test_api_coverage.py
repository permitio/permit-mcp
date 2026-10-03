"""Tests for api_coverage.py, the API coverage report and spec drift check.

They run the script on the committed inventories and allowlist, or on planted copies,
with planted request records and origins files shaped as tests/api_record.py writes them.

Run with:
uv run --only-dev pytest -c .github/scripts/pytest.ini .github/scripts/test_api_coverage.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

# Planted edits: of the allowlist, of an inventory's operations, and a planted inventory.
Edit = Callable[[dict[str, Any]], object]
OpsEdit = Callable[[list[dict[str, Any]]], object]
Plant = Callable[[Path], Path]

SCRIPTS = Path(__file__).resolve().parent
SCRIPT = SCRIPTS / "api_coverage.py"
SPECS = SCRIPTS.parent / "api-specs"
CONTROL_PLANE = SPECS / "control-plane.json"
PDP = SPECS / "pdp.json"
ALLOWLIST = SCRIPTS / "api_coverage_allowlist.json"

sys.path.insert(0, str(SCRIPTS))

import api_coverage  # noqa: E402 - importable once sys.path has its directory

API_ORIGIN = "http://127.0.0.1:41001"
PDP_ORIGIN = "http://127.0.0.1:41002"
BOTH_ORIGIN = "http://127.0.0.1:41003"
ORIGINS = {
    API_ORIGIN: ["control-plane"],
    PDP_ORIGIN: ["pdp"],
    BOTH_ORIGIN: ["control-plane", "pdp"],
}

FACTS = "/v2/facts/proj-id/env-id"
AR = f"{FACTS}/access_requests/ar-elem/user/alice/tenant/acme"
AR_ID = "6f1c2a9e-3b7d-4c55-9a0e-1d2b3c4d5e6f"
OA = "/v2/elements/proj-id/env-id/config/oa-elem/operation_approval"
OA_ID = "0b9e8d7c-6a5f-4e3d-8c2b-1a0f9e8d7c6b"
AR_ELEMENTS = "/v2/elements/proj-id/env-id/config/ar-elem/access_requests"
LOGIN = ("POST", "/v2/auth/elements_login_as")
SCOPE = ("GET", "/v2/api-key/scope")
CHECK = ("POST", "/allowed")

# The requests each tool's wire case in tests/support.py sends, after the scope lookup.
WIRE_CASES: dict[str, list[tuple[str, str]]] = {
    "list_resource_instances": [("GET", f"{FACTS}/resource_instances")],
    "check_permission": [CHECK],
    "create_access_request": [("POST", AR)],
    "list_access_requests": [("GET", AR)],
    "approve_access_request": [("PUT", f"{AR}/{AR_ID}/approve")],
    "deny_access_request": [("PUT", f"{AR}/{AR_ID}/deny")],
    "cancel_access_request": [LOGIN, ("PUT", f"{AR_ELEMENTS}/{AR_ID}/cancel")],
    "create_operation_approval": [LOGIN, ("POST", OA)],
    "list_operation_approvals": [LOGIN, ("GET", OA)],
    "approve_operation_approval": [LOGIN, ("PUT", f"{OA}/{OA_ID}/approve")],
    "deny_operation_approval": [LOGIN, ("PUT", f"{OA}/{OA_ID}/deny")],
    "cancel_operation_approval": [LOGIN, ("PUT", f"{OA}/{OA_ID}/cancel")],
}
OA_TEMPLATE = "/v2/elements/{proj_id}/{env_id}/config/{elements_config_id}/operation_approval"
DENY_OA = f"PUT {OA_TEMPLATE}/{{operation_approval_id}}/deny"
GET_AR = (
    "GET /v2/facts/{proj_id}/{env_id}/access_requests/{elements_config_id}/user/{user_id}"
    "/tenant/{tenant_id}/{access_request_id}"
)
UNEXPLAINED = "no test sends it and the allowlist gives no reason"


def line(
    method: str,
    path: str,
    test: str | None,
    status: int | None = 200,
    origin: str = API_ORIGIN,
) -> dict[str, Any]:
    return {"method": method, "origin": origin, "path": path, "status": status, "test": test}


def wire_record(skip: str | None = None) -> list[dict[str, Any]]:
    """A record of every wire case but `skip`, padded with scope lookups to the minimum."""
    lines: list[dict[str, Any]] = []
    for tool, calls in WIRE_CASES.items():
        if tool == skip:
            continue
        test = (
            f"tests/test_tools_wire.py::test_tool_sends_exact_request_and_returns_api_json[{tool}]"
        )
        lines.append(line(*SCOPE, test))
        for method, path in calls:
            lines.append(
                line(method, path, test, origin=PDP_ORIGIN if path == "/allowed" else API_ORIGIN)
            )
    padding = api_coverage.MIN_REQUESTS - len(lines)
    lines += [line(*SCOPE, f"tests/test_session.py::test_scope[{n}]") for n in range(padding)]
    return lines


def write_record(path: Path, lines: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(item) + "\n" for item in lines), encoding="utf-8")
    return path


def read(path: Path) -> Any:  # noqa: ANN401 - planted JSON documents
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, document: object) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def plant_inventory(directory: Path, name: str, document: object) -> Path:
    """Write an inventory and a source file beside it, as `snapshot` does."""
    directory.mkdir(parents=True, exist_ok=True)
    inventory = write_json(directory / f"{name}.json", document)
    source = {"source": "https://example.test/openapi.json", "fetched": "2026-10-03"}
    write_json(directory / f"{name}.source.json", source)
    return inventory


def run(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
    )


def report_args(  # noqa: PLR0913 - keyword-only, one per input
    record: Path,
    *,
    control_plane: Path = CONTROL_PLANE,
    pdp: Path = PDP,
    allowlist: Path = ALLOWLIST,
    origins: Path | None = None,
    extra: tuple[str | Path, ...] = (),
) -> list[str | Path]:
    if origins is None:
        origins = write_json(record.with_name("origins.json"), ORIGINS)
    return [
        "report",
        "--spec",
        f"control-plane={control_plane}",
        "--spec",
        f"pdp={pdp}",
        "--allowlist",
        allowlist,
        "--record",
        record,
        "--origins",
        origins,
        *extra,
    ]


def report(  # noqa: PLR0913 - keyword-only, one per input
    record: Path,
    *,
    control_plane: Path = CONTROL_PLANE,
    pdp: Path = PDP,
    allowlist: Path = ALLOWLIST,
    origins: Path | None = None,
    extra: tuple[str | Path, ...] = (),
) -> subprocess.CompletedProcess[str]:
    return run(
        *report_args(
            record,
            control_plane=control_plane,
            pdp=pdp,
            allowlist=allowlist,
            origins=origins,
            extra=extra,
        )
    )


@pytest.fixture
def record(tmp_path: Path) -> Path:
    return write_record(tmp_path / "record.jsonl", wire_record())


@pytest.fixture
def allowlist() -> dict[str, Any]:
    document: dict[str, Any] = read(ALLOWLIST)
    return document


def entry(allowlist: dict[str, Any], operation: str) -> dict[str, Any]:
    found: list[dict[str, Any]] = [
        item for item in allowlist["operations"] if item["operation"] == operation
    ]
    assert len(found) == 1, operation
    return found[0]


# --- the committed inputs ------------------------------------------------------------


def test_the_wire_cases_cover_the_committed_scope(record: Path) -> None:
    result = report(record)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "| Control plane | 23 | 12 | 0 | 11 |" in result.stdout
    assert "| PDP | 1 | 1 | 0 | 0 |" in result.stdout
    assert "SDK-only requests: 1." in result.stdout
    assert "get access request: not exposed as a tool" in result.stdout
    assert "the facts route is used instead" in result.stdout
    assert "| `Control plane POST /v2/auth/elements_login_as` | 6 | The Elements login" in (
        result.stdout
    )
    assert "Out of scope: permit-mcp exposes" in result.stdout
    assert "taken from https://api.permit.io/v2/openapi.json on " in result.stdout
    assert "0 requests got no response" in result.stdout
    assert "::error" not in result.stdout


def test_the_committed_inventories_are_what_snapshot_writes() -> None:
    for inventory in (CONTROL_PLANE, PDP):
        document = read(inventory)
        assert set(document) == {"operations"}
        keys = [(op["path"], op["method"]) for op in document["operations"]]
        assert keys == sorted(keys), f"{inventory} is not sorted as snapshot sorts it"
        for operation in document["operations"]:
            assert set(operation) == api_coverage.INVENTORY_FIELDS
        assert set(read(api_coverage.source_file(inventory))) == {"source", "fetched"}
    assert read(api_coverage.source_file(CONTROL_PLANE))["source"] == (
        "https://api.permit.io/v2/openapi.json"
    )
    assert len(read(CONTROL_PLANE)["operations"]) == 23
    assert read(PDP)["operations"] == [
        {
            "deprecated": False,
            "method": "POST",
            "operationId": "is_allowed_allowed_post",
            "parameters": [{"in": "header", "name": "x-permit-sdk-language", "required": False}],
            "path": "/allowed",
            "requestBody": "Query",
            "tags": ["Authorization API"],
        }
    ]


# --- the gate fails --------------------------------------------------------------------


def test_deleting_a_wire_case_fails_the_report(tmp_path: Path) -> None:
    record = write_record(tmp_path / "record.jsonl", wire_record(skip="deny_operation_approval"))
    result = report(record)
    assert result.returncode == 1
    assert f"- `Control plane {DENY_OA}`: {UNEXPLAINED}" in result.stdout
    assert "| Control plane | 23 | 11 | 0 | 12 |" in result.stdout
    assert "::error title=API coverage::unexplained: Control plane PUT" in result.stdout


@pytest.mark.parametrize("reason", [None, "", "  "], ids=["dropped", "empty", "blank"])
def test_an_entry_without_a_reason_fails_the_report(
    tmp_path: Path, record: Path, allowlist: dict[str, Any], reason: str | None
) -> None:
    item = entry(allowlist, GET_AR)
    if reason is None:
        del item["reason"]
    else:
        item["reason"] = reason
    result = report(record, allowlist=write_json(tmp_path / "allowlist.json", allowlist))
    assert result.returncode == 1
    assert f"- `Control plane {GET_AR}`: {UNEXPLAINED}" in result.stdout


def test_an_sdk_only_entry_without_a_reason_fails_the_report(
    tmp_path: Path, record: Path, allowlist: dict[str, Any]
) -> None:
    del allowlist["sdk_only"][0]["reason"]
    result = report(record, allowlist=write_json(tmp_path / "allowlist.json", allowlist))
    assert result.returncode == 1
    assert "`Control plane POST /v2/auth/elements_login_as`: the sdk_only entry has no" in (
        result.stdout
    )


def test_an_untested_operation_with_a_reason_passes(
    tmp_path: Path, allowlist: dict[str, Any]
) -> None:
    lines = [item for item in wire_record() if "/resource_instances" not in item["path"]]
    record = write_record(tmp_path / "record.jsonl", [*lines, line(*SCOPE, "pad")])
    allowlist["operations"].append(
        {
            "api": "control-plane",
            "operation": "GET /v2/facts/{proj_id}/{env_id}/resource_instances",
            "status": "untested",
            "reason": "planted",
        }
    )
    result = report(record, allowlist=write_json(tmp_path / "allowlist.json", allowlist))
    assert result.returncode == 0, result.stdout
    assert "| Control plane | 23 | 11 | 1 | 11 |" in result.stdout
    assert "| untested | planted |" in result.stdout


def test_a_request_to_an_out_of_scope_operation_fails(tmp_path: Path) -> None:
    lines = [*wire_record(), line("GET", f"{FACTS}/users", "tests/test_x.py::test_list")]
    result = report(write_record(tmp_path / "record.jsonl", lines))
    assert result.returncode == 1
    assert (
        f"- `GET {FACTS}/users to {API_ORIGIN} (Control plane), sent by "
        "tests/test_x.py::test_list`: matches no in-scope operation and no sdk_only entry: "
        "add it to the scope or to sdk_only"
    ) in result.stdout


def test_an_unexplained_sdk_only_request_fails(tmp_path: Path) -> None:
    lines = [*wire_record(), line("POST", "/v2/unknown", "tests/test_x.py::test_new")]
    result = report(write_record(tmp_path / "record.jsonl", lines))
    assert result.returncode == 1
    assert "`POST /v2/unknown to http://127.0.0.1:41001 (Control plane), sent by" in result.stdout


# --- only real responses, from the right API, count ---------------------------------------


def test_requests_that_got_no_response_cover_nothing(tmp_path: Path) -> None:
    lines = wire_record()
    for item in lines:
        if item["method"] == "PUT" and item["path"].endswith(f"{OA_ID}/deny"):
            item["status"] = None
    lines.append(line("POST", "/v2/never-answered", "tests/x.py::t", status=None))
    result = report(write_record(tmp_path / "record.jsonl", lines))
    assert result.returncode == 1
    assert f"- `Control plane {DENY_OA}`: {UNEXPLAINED}" in result.stdout
    assert "2 requests got no response and count for nothing" in result.stdout
    assert "never-answered" not in result.stdout


def test_a_check_sent_to_the_api_mock_does_not_cover_the_pdp(tmp_path: Path) -> None:
    lines = wire_record()
    for item in lines:
        if item["path"] == "/allowed":
            item["origin"] = API_ORIGIN
    result = report(write_record(tmp_path / "record.jsonl", lines))
    assert result.returncode == 1
    assert f"- `PDP POST /allowed`: {UNEXPLAINED}" in result.stdout
    assert "| PDP | 1 | 0 | 0 | 1 |" in result.stdout
    assert f"`POST /allowed to {API_ORIGIN} (Control plane), sent by" in result.stdout


def test_a_request_to_an_unknown_origin_is_unmatched(tmp_path: Path) -> None:
    lines = [*wire_record(), line(*CHECK, "tests/x.py::t", origin="http://elsewhere.test")]
    result = report(write_record(tmp_path / "record.jsonl", lines))
    assert result.returncode == 1
    assert "`POST /allowed to http://elsewhere.test (no API in the origins file)" in result.stdout


def test_an_origin_that_serves_both_apis_covers_both(tmp_path: Path) -> None:
    lines = [{**item, "origin": BOTH_ORIGIN} for item in wire_record()]
    result = report(write_record(tmp_path / "record.jsonl", lines))
    assert result.returncode == 0, result.stdout
    assert "| PDP | 1 | 1 | 0 | 0 |" in result.stdout


def test_an_sdk_only_entry_matches_only_its_api(tmp_path: Path) -> None:
    lines = [
        {**item, "origin": PDP_ORIGIN} if item["path"] == LOGIN[1] else item
        for item in wire_record()
    ]
    result = report(write_record(tmp_path / "record.jsonl", lines))
    assert result.returncode == 1
    assert "an sdk_only entry no request matches" in result.stdout
    assert f"`POST /v2/auth/elements_login_as to {PDP_ORIGIN} (PDP), sent by" in result.stdout


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda a: a["operations"].append(
                {"api": "control-plane", "operation": "GET /v2/api-key/scope", "status": "missing"}
            ),
            "`Control plane GET /v2/api-key/scope`: allowlisted as missing",
        ),
        (
            lambda a: a["operations"].append(
                {"api": "control-plane", "operation": "GET /v2/projects", "status": "missing"}
            ),
            "`Control plane GET /v2/projects`: allowlisted, but not in scope",
        ),
        (
            lambda a: a["sdk_only"].append(
                {"api": "control-plane", "request": "GET /v2/never", "reason": "planted"}
            ),
            "`Control plane GET /v2/never`: an sdk_only entry no request matches",
        ),
        (
            lambda a: a["scope"]["control-plane"]["tags"].append("Gone (EAP)"),
            "`Control plane tag Gone (EAP)`: no operation has this tag",
        ),
        (
            lambda a: a["scope"]["control-plane"]["tags"].append("Access Requests (EAP)"),
            "`Control plane tag Access Requests (EAP)`: the scope lists it twice",
        ),
        (
            lambda a: a["scope"]["pdp"]["operations"].append("POST /gone"),
            "`PDP POST /gone`: a scope operation not in the inventory",
        ),
        (
            lambda a: a["scope"]["pdp"]["operations"].append("POST /allowed"),
            "`PDP POST /allowed`: the scope lists it twice",
        ),
        (
            lambda a: a["scope"]["control-plane"]["operations"].append(GET_AR),
            f"`Control plane {GET_AR}`: a scope operation a scope tag already has",
        ),
        (
            lambda a: a["scope"]["control-plane"]["operations"].remove("GET /v2/api-key/scope"),
            "`Control plane GET /v2/api-key/scope`: in the inventory but not in the scope",
        ),
    ],
    ids=[
        "entry for a covered operation",
        "entry not in scope",
        "sdk_only entry unmatched",
        "scope tag not in the inventory",
        "scope tag twice",
        "scope operation not in the inventory",
        "scope operation twice",
        "scope operation in a scope tag",
        "inventory operation out of scope",
    ],
)
def test_a_stale_allowlist_entry_fails(
    tmp_path: Path, record: Path, allowlist: dict[str, Any], change: Edit, message: str
) -> None:
    change(allowlist)
    result = report(record, allowlist=write_json(tmp_path / "allowlist.json", allowlist))
    assert result.returncode == 1, result.stdout
    assert "### Stale allowlist entries" in result.stdout
    assert f"- {message}" in result.stdout


# --- matching ----------------------------------------------------------------------------


def test_the_template_with_the_most_literal_segments_wins(tmp_path: Path) -> None:
    document = read(CONTROL_PLANE)
    by_id = {**document["operations"][0], "path": "/v2/api-key/{api_key_id}", "operationId": "x"}
    document["operations"].append(by_id)
    inventory = api_coverage.load_inventory(
        "control-plane", plant_inventory(tmp_path, "control-plane", document)
    )
    scope = inventory.match("GET", "/v2/api-key/scope")
    assert scope is not None
    assert scope.operation_id == "get_api_key_scope"
    other = inventory.match("GET", "/v2/api-key/some-key-id")
    assert other is not None
    assert other.operation_id == "x"


def test_an_escaped_slash_stays_inside_its_segment() -> None:
    inventory = api_coverage.load_inventory("control-plane", CONTROL_PLANE)
    approval = inventory.match("GET", f"{OA}/a%2Fb")
    assert approval is not None
    assert approval.operation_id == "get_operation_approval"
    assert inventory.match("GET", f"{OA}/a/b") is None
    assert inventory.match("GET", f"{OA}/{OA_ID}/extra") is None


def test_the_method_must_match() -> None:
    inventory = api_coverage.load_inventory("pdp", PDP)
    assert inventory.match("GET", "/allowed") is None


# --- spec drift ---------------------------------------------------------------------------


def drifted(tmp_path: Path, change: OpsEdit) -> Path:
    document = read(CONTROL_PLANE)
    change(document["operations"])
    return plant_inventory(tmp_path / "live", "control-plane", document)


def with_operation_id(operations: list[dict[str, Any]], operation_id: str) -> dict[str, Any]:
    found = [op for op in operations if op["operationId"] == operation_id]
    assert len(found) == 1, operation_id
    return found[0]


def rename_parameter(operations: list[dict[str, Any]]) -> None:
    listing = with_operation_id(operations, "list_access_requests")
    for parameter in listing["parameters"]:
        if parameter["name"] == "resource_instance_id":
            parameter["name"] = "resource_instance"


NEW_OPERATION = {
    "method": "DELETE",
    "path": f"{OA_TEMPLATE}/{{id}}",
    "operationId": "delete_operation_approval",
    "tags": ["Operation Approval (EAP)"],
    "deprecated": False,
    "parameters": [],
    "requestBody": None,
}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda ops: ops.append(NEW_OPERATION), "added since the snapshot"),
        (
            lambda ops: ops.remove(with_operation_id(ops, "create_operation_approval")),
            "removed since the snapshot",
        ),
        (
            lambda ops: with_operation_id(ops, "list_access_requests").update(deprecated=True),
            "deprecated False -> True",
        ),
        (
            lambda ops: with_operation_id(ops, "get_operation_approval").update(
                operationId="fetch_operation_approval"
            ),
            "operationId 'get_operation_approval' -> 'fetch_operation_approval'",
        ),
        (
            lambda ops: with_operation_id(ops, "deny_operation_approval").update(tags=["Other"]),
            "tags ['Operation Approval (EAP)'] -> ['Other']",
        ),
        (
            lambda ops: with_operation_id(ops, "get_operation_approval").update(
                path=f"{OA_TEMPLATE}/{{approval_id}}"
            ),
            f"path '{OA_TEMPLATE}/{{operation_approval_id}}' -> ",
        ),
        (rename_parameter, "'query resource_instance_id'"),
        (
            lambda ops: with_operation_id(ops, "list_operation_approvals")["parameters"].append(
                {"in": "query", "name": "tenant", "required": True}
            ),
            "'query tenant (required)'",
        ),
        (
            lambda ops: with_operation_id(ops, "deny_access_request").update(
                requestBody="#/components/schemas/AccessRequestReview"
            ),
            (
                "requestBody '#/components/schemas/AccessRequestReviewDeny' -> "
                "'#/components/schemas/AccessRequestReview'"
            ),
        ),
    ],
    ids=[
        "added",
        "removed",
        "deprecated",
        "operationId",
        "tag",
        "path",
        "renamed query parameter",
        "new required parameter",
        "request body",
    ],
)
def test_in_scope_drift_from_the_baseline_fails(
    tmp_path: Path, record: Path, change: OpsEdit, message: str
) -> None:
    live = drifted(tmp_path, change)
    baseline = ("--baseline", f"control-plane={CONTROL_PLANE}")
    result = report(record, control_plane=live, extra=baseline)
    assert result.returncode == 1, result.stdout
    assert "### In-scope operations that changed since the snapshot" in result.stdout
    assert message in result.stdout
    assert "- Control plane baseline: `" in result.stdout


def test_a_renamed_query_parameter_is_named_in_the_drift(tmp_path: Path, record: Path) -> None:
    live = drifted(tmp_path, rename_parameter)
    baseline = ("--baseline", f"control-plane={CONTROL_PLANE}")
    result = report(record, control_plane=live, extra=baseline)
    assert result.returncode == 1
    drift = [text for text in result.stdout.splitlines() if text.startswith("::error")]
    assert len(drift) == 1
    assert "drift: Control plane GET /v2/facts/{proj_id}/{env_id}/access_requests/" in drift[0]
    assert "'query resource_instance_id'" in drift[0]
    assert "'query resource_instance'" in drift[0]


def test_no_drift_passes(record: Path) -> None:
    result = report(record, extra=("--baseline", f"control-plane={CONTROL_PLANE}"))
    assert result.returncode == 0, result.stdout


# --- the report did not run ----------------------------------------------------------------


def without_source(tmp_path: Path) -> Path:
    inventory = plant_inventory(tmp_path, "control-plane", read(CONTROL_PLANE))
    api_coverage.source_file(inventory).unlink()
    return inventory


def duplicated(tmp_path: Path) -> Path:
    document = read(CONTROL_PLANE)
    renamed = {**document["operations"][0], "path": document["operations"][0]["path"] + "x"}
    document["operations"] += [document["operations"][0], renamed]
    return plant_inventory(tmp_path, "control-plane", document)


def missing_field(tmp_path: Path) -> Path:
    document = read(CONTROL_PLANE)
    del document["operations"][0]["parameters"]
    return plant_inventory(tmp_path, "control-plane", document)


def wrong_type(tmp_path: Path) -> Path:
    document = read(CONTROL_PLANE)
    document["operations"][0]["parameters"] = [{"in": "query", "name": "x", "required": "no"}]
    return plant_inventory(tmp_path, "control-plane", document)


@pytest.mark.parametrize(
    ("plant", "message"),
    [
        (lambda tmp: tmp / "absent.json", "could not read the Control plane inventory"),
        (
            lambda tmp: plant_inventory(tmp, "control-plane", "not an object"),
            "has no `operations` list",
        ),
        (
            lambda tmp: plant_inventory(tmp, "control-plane", {"operations": []}),
            "lists no operation",
        ),
        (without_source, "could not read the source file"),
        (duplicated, "twice"),
        (missing_field, "needs exactly deprecated, method, operationId, parameters, path"),
        (wrong_type, "has a field of the wrong type"),
    ],
    ids=["missing", "not an inventory", "empty", "no source", "duplicate", "field", "type"],
)
def test_an_unusable_inventory_exits_2(
    tmp_path: Path, record: Path, plant: Plant, message: str
) -> None:
    result = report(record, control_plane=plant(tmp_path))
    assert result.returncode == 2
    assert message in result.stdout
    assert "The report did not run" in result.stdout


def test_a_source_file_without_a_date_exits_2(tmp_path: Path, record: Path) -> None:
    inventory = plant_inventory(tmp_path, "control-plane", read(CONTROL_PLANE))
    write_json(api_coverage.source_file(inventory), {"source": "https://example.test"})
    result = report(record, control_plane=inventory)
    assert result.returncode == 2
    assert 'needs a "source" and a "fetched" string' in result.stdout


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ("", "holds 0 requests, fewer than the minimum of 200"),
        (
            "".join(json.dumps(item) + "\n" for item in wire_record()[1:]),
            "holds 199 requests, fewer than the minimum of 200",
        ),
        ("not json\n", "line 1 of the request record"),
        (json.dumps(["GET", "/v2"]) + "\n", "is not a request line"),
        (
            json.dumps({**line(*SCOPE, "t"), "authorization": "Bearer x"}) + "\n",
            "is not a request line of method, origin, path, status, test",
        ),
        (
            json.dumps({key: value for key, value in line(*SCOPE, "t").items() if key != "origin"})
            + "\n",
            "is not a request line of method, origin, path, status, test",
        ),
        (json.dumps(line("GET", "v2/no-slash", "t")) + "\n", "is not a well-formed request"),
        (json.dumps(line("GET", "/v2", "t", status=True)) + "\n", "is not a well-formed"),
    ],
    ids=[
        "empty",
        "below the minimum",
        "not JSON",
        "not an object",
        "extra field",
        "no origin",
        "path",
        "status",
    ],
)
def test_an_unusable_record_exits_2(tmp_path: Path, lines: str, message: str) -> None:
    record = tmp_path / "record.jsonl"
    record.write_text(lines, encoding="utf-8")
    result = report(record)
    assert result.returncode == 2
    assert message in result.stdout


def test_a_missing_record_exits_2(tmp_path: Path) -> None:
    result = report(tmp_path / "absent.jsonl")
    assert result.returncode == 2
    assert "could not read the request record" in result.stdout


@pytest.mark.parametrize(
    ("origins", "message"),
    [
        (None, "could not read the origins file"),
        (["http://x"], "must map each origin to a list of control-plane, pdp"),
        ({API_ORIGIN: []}, "must map each origin to a list"),
        ({API_ORIGIN: ["control-plane", "other"]}, "must map each origin to a list"),
        ({API_ORIGIN: ["control-plane"]}, "names no origin for the PDP"),
    ],
    ids=["missing", "not an object", "no API", "unknown API", "no PDP origin"],
)
def test_an_unusable_origins_file_exits_2(record: Path, origins: object, message: str) -> None:
    path = record.with_name("planted-origins.json")
    if origins is not None:
        write_json(path, origins)
    result = report(record, origins=path)
    assert result.returncode == 2
    assert message in result.stdout


def test_a_request_outside_any_test_is_attributed(tmp_path: Path) -> None:
    lines = [*wire_record(), line("POST", "/v2/unknown", None)]
    result = report(write_record(tmp_path / "record.jsonl", lines))
    assert result.returncode == 1
    assert "sent by (outside any test)" in result.stdout


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda a: a.pop("sdk_only"), "must be an object of"),
        (lambda a: a.update(out_of_scope=" "), 'needs an "out_of_scope" reason'),
        (lambda a: a["scope"].pop("pdp"), 'needs a "scope" object with'),
        (lambda a: a["scope"]["pdp"].update(tags="x"), "tags must be a list of str"),
        (lambda a: a["scope"]["pdp"]["operations"].append("post /x"), "is not an upper-case"),
        (lambda a: a["operations"][0].update(status="excluded"), '"status" must be one of'),
        (lambda a: a["operations"][0].update(api="other"), '"api" must be one of'),
        (lambda a: a["sdk_only"][0].pop("api"), '"api" must be one of'),
        (lambda a: a["operations"][0].update(reason=1), '"reason" must be a string'),
        (lambda a: a["operations"].append(a["operations"][0]), "is listed twice"),
        (lambda a: a["sdk_only"].append(a["sdk_only"][0]), "is listed twice"),
        (lambda a: a["sdk_only"][0].pop("request"), "needs a request such as"),
    ],
    ids=[
        "missing key",
        "no out-of-scope reason",
        "scope missing an API",
        "tags not a list",
        "lower-case method",
        "unknown status",
        "unknown api",
        "sdk_only without an api",
        "reason not a string",
        "repeated entry",
        "repeated sdk_only",
        "sdk_only without a request",
    ],
)
def test_a_malformed_allowlist_exits_2(
    tmp_path: Path, record: Path, allowlist: dict[str, Any], change: Edit, message: str
) -> None:
    change(allowlist)
    result = report(record, allowlist=write_json(tmp_path / "allowlist.json", allowlist))
    assert result.returncode == 2
    assert message in result.stdout


def test_an_unreadable_allowlist_exits_2(tmp_path: Path, record: Path) -> None:
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text("{", encoding="utf-8")
    result = report(record, allowlist=allowlist)
    assert result.returncode == 2
    assert "is not JSON" in result.stdout


def test_an_unexpected_error_exits_2_and_says_the_report_did_not_run(
    tmp_path: Path,
    record: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def broken(path: Path) -> list[api_coverage.Request]:
        msg = f"planted failure reading {path.name}"
        raise RuntimeError(msg)

    monkeypatch.setattr(api_coverage, "load_record", broken)
    summary = tmp_path / "summary.md"
    args = report_args(record, extra=("--summary", summary))
    assert api_coverage.main([str(arg) for arg in args]) == 2
    text = summary.read_text(encoding="utf-8")
    assert ":warning: **The report did not run**" in text
    assert "RuntimeError: planted failure reading record.jsonl" in text
    captured = capsys.readouterr()
    assert captured.out.startswith("::error title=API coverage::The report did not run")
    assert "Traceback" in captured.err


@pytest.mark.parametrize(
    "spec_args",
    [
        ("--spec", f"control-plane={CONTROL_PLANE}"),
        ("--spec", f"pdp={PDP}", "--spec", f"pdp={PDP}", "--spec", f"control-plane={PDP}"),
        ("--spec", f"other={PDP}", "--spec", f"pdp={PDP}"),
    ],
    ids=["one missing", "repeated", "unknown"],
)
def test_wrong_spec_arguments_exit_2(record: Path, spec_args: tuple[str, ...]) -> None:
    origins = write_json(record.with_name("origins.json"), ORIGINS)
    result = run(
        "report", *spec_args, "--allowlist", ALLOWLIST, "--record", record, "--origins", origins
    )
    assert result.returncode == 2
    assert "--spec" in result.stdout


def test_a_pdp_baseline_is_refused(record: Path) -> None:
    result = report(record, extra=("--baseline", f"pdp={PDP}"))
    assert result.returncode == 2
    assert "--baseline takes NAME=PATH" in result.stdout


def test_bad_arguments_exit_2() -> None:
    assert run().returncode == 2
    assert run("report").returncode == 2


# --- the summary -------------------------------------------------------------------------


def test_the_summary_is_appended_to_the_file(tmp_path: Path, record: Path) -> None:
    summary = tmp_path / "summary.md"
    summary.write_text("earlier step\n", encoding="utf-8")
    result = report(record, extra=("--summary", summary))
    assert result.returncode == 0
    assert result.stdout == ""
    text = summary.read_text(encoding="utf-8")
    assert text.startswith("earlier step\n## API coverage\n")
    assert ":white_check_mark:" in text


def test_a_report_that_did_not_run_says_so_in_the_summary(tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"
    result = report(tmp_path / "absent.jsonl", extra=("--summary", summary))
    assert result.returncode == 2
    assert ":warning: **The report did not run**" in summary.read_text(encoding="utf-8")
    assert result.stdout.startswith("::error title=API coverage::The report did not run")


def test_outside_text_cannot_break_the_table_or_the_annotation(tmp_path: Path) -> None:
    lines = [*wire_record(), line("POST", "/v2/a|b`c", "tests/x.py::t[%0A::warning::]")]
    result = report(write_record(tmp_path / "record.jsonl", lines))
    assert result.returncode == 1
    assert "`POST /v2/a\\|b'c to " in result.stdout
    annotations = [text for text in result.stdout.splitlines() if text.startswith("::")]
    assert len(annotations) == 1
    assert annotations[0].startswith("::error title=API coverage::unmatched: POST /v2/a|b`c to ")
    assert "sent by tests/x.py::t[%250A::warning::]: matches no" in annotations[0]


# --- snapshot --------------------------------------------------------------------------------

THINGS = "/v2/things/{thing_id}"


def openapi(operations: int) -> dict[str, Any]:
    """A planted OpenAPI document with `operations` operations, two of them in scope."""
    paths: dict[str, Any] = {
        f"/v2/filler/{n}": {"get": {"operationId": f"filler_{n}", "tags": ["Filler"]}}
        for n in range(operations - 2)
    }
    paths[THINGS] = {
        "parameters": [{"$ref": "#/components/parameters/ThingId"}],
        "put": {
            "operationId": "replace_thing",
            "tags": ["Things (EAP)"],
            "summary": "drop me",
            "parameters": [{"name": "force", "in": "query", "required": True}],
            "requestBody": {"$ref": "#/components/requestBodies/Thing"},
            "responses": {"200": {"description": "drop me"}},
        },
    }
    paths["/v2/old"] = {"delete": {"deprecated": True, "tags": ["Old"]}}
    components = {
        "parameters": {"ThingId": {"name": "thing_id", "in": "path", "required": True}},
        "requestBodies": {
            "Thing": {"content": {"application/json": {"schema": {"$ref": "#/S/Thing"}}}}
        },
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": "drop me"},
        "paths": paths,
        "components": components,
    }


@pytest.fixture
def things_allowlist(tmp_path: Path, allowlist: dict[str, Any]) -> Path:
    allowlist["scope"]["control-plane"] = {
        "tags": ["Things (EAP)"],
        "operations": ["DELETE /v2/old"],
    }
    return write_json(tmp_path / "things-allowlist.json", allowlist)


def snapshot(spec: Path, allowlist: Path, out: Path, api: str = "control-plane") -> Any:  # noqa: ANN401
    return run(
        "snapshot",
        api,
        spec,
        "--allowlist",
        allowlist,
        "--source",
        "https://x.test/o.json",
        "--out-dir",
        out,
    )


def test_snapshot_keeps_the_in_scope_operations_and_their_wire_shape(
    tmp_path: Path, things_allowlist: Path
) -> None:
    spec = write_json(tmp_path / "openapi.json", openapi(200))
    out = tmp_path / "out"
    result = snapshot(spec, things_allowlist, out)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Wrote 2 in-scope operations of the 200 operations" in result.stdout
    inventory = read(out / "control-plane.json")
    assert "drop me" not in json.dumps(inventory)
    assert "filler" not in json.dumps(inventory)
    assert inventory["operations"] == [
        {
            "deprecated": True,
            "method": "DELETE",
            "operationId": "",
            "parameters": [],
            "path": "/v2/old",
            "requestBody": None,
            "tags": ["Old"],
        },
        {
            "deprecated": False,
            "method": "PUT",
            "operationId": "replace_thing",
            "parameters": [
                {"in": "path", "name": "thing_id", "required": True},
                {"in": "query", "name": "force", "required": True},
            ],
            "path": THINGS,
            "requestBody": "#/S/Thing",
            "tags": ["Things (EAP)"],
        },
    ]
    assert read(out / "control-plane.source.json")["source"] == "https://x.test/o.json"
    loaded = api_coverage.load_inventory("control-plane", out / "control-plane.json")
    assert len(loaded.operations) == 2


def test_snapshot_dates_the_source_today_unless_told(tmp_path: Path) -> None:
    spec = write_json(tmp_path / "openapi.json", openapi(3))
    run(
        "snapshot",
        "pdp",
        spec,
        "--allowlist",
        ALLOWLIST,
        "--source",
        "s",
        "--fetched",
        "2026-01-02",
        "--out-dir",
        tmp_path,
    )
    assert read(tmp_path / "pdp.source.json") == {"source": "s", "fetched": "2026-01-02"}


def as_openapi(inventory: Path, filler: int) -> dict[str, Any]:
    """An OpenAPI document holding an inventory's operations and `filler` out-of-scope ones."""
    paths: dict[str, Any] = {
        f"/v2/filler/{n}": {"get": {"operationId": f"f{n}", "tags": ["Filler"]}}
        for n in range(filler)
    }
    for op in read(inventory)["operations"]:
        body = op["requestBody"]
        paths.setdefault(op["path"], {})[op["method"].lower()] = {
            "operationId": op["operationId"],
            "tags": op["tags"],
            "deprecated": op["deprecated"],
            "parameters": op["parameters"],
            **(
                {"requestBody": {"content": {"application/json": {"schema": {"$ref": body}}}}}
                if body
                else {}
            ),
        }
    return {"paths": paths}


def test_a_snapshot_round_trips_through_the_report(tmp_path: Path, record: Path) -> None:
    spec = write_json(tmp_path / "openapi.json", as_openapi(CONTROL_PLANE, filler=200))
    out = tmp_path / "out"
    assert snapshot(spec, ALLOWLIST, out).returncode == 0
    assert read(out / "control-plane.json") == read(CONTROL_PLANE)
    result = report(record, control_plane=out / "control-plane.json")
    assert result.returncode == 0


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (None, "could not read the Control plane spec"),
        ({"openapi": "3.1.0"}, "has no `paths` object"),
        (openapi(199), "lists 199 operations, fewer than the minimum of 200"),
        (
            {"paths": {"/x": {"get": {"parameters": [{"$ref": "#/components/parameters/X"}]}}}},
            "the spec has no #/components/parameters/X",
        ),
    ],
    ids=["missing", "no paths", "truncated", "dangling ref"],
)
def test_snapshot_of_an_unusable_spec_exits_2(
    tmp_path: Path, document: object, message: str
) -> None:
    spec = tmp_path / "openapi.json"
    if document is not None:
        write_json(spec, document)
    out = tmp_path / "out"
    result = snapshot(spec, ALLOWLIST, out)
    assert result.returncode == 2
    assert message in result.stdout
    assert not out.exists()


def test_snapshot_with_an_unusable_allowlist_exits_2(tmp_path: Path) -> None:
    spec = write_json(tmp_path / "openapi.json", openapi(200))
    result = snapshot(spec, tmp_path / "absent.json", tmp_path / "out")
    assert result.returncode == 2
    assert "could not read the allowlist" in result.stdout
