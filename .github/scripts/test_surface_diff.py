"""Tests for surface_diff.py, which labels surface snapshot changes BREAKING or non-breaking.

Each rule is planted in both directions on a small snapshot. The committed snapshot is
compared with itself, and with copies changed the way a pull request would change it.

Run with:
uv run --only-dev pytest -c .github/scripts/pytest.ini .github/scripts/test_surface_diff.py
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

SCRIPTS = Path(__file__).resolve().parent
SCRIPT = SCRIPTS / "surface_diff.py"
COMMITTED = SCRIPTS.parents[1] / "tests" / "snapshots" / "surface.json"

sys.path.insert(0, str(SCRIPTS))

from surface_diff import diff  # noqa: E402 - importable once sys.path has its directory


def base() -> dict[str, Any]:
    return {
        "tools": {
            "list_items": {
                "description": "List the items.",
                "input_schema": {
                    "type": "object",
                    "title": "list_itemsArguments",
                    "properties": {
                        "status": {
                            "anyOf": [
                                {"enum": ["open", "closed"], "type": "string"},
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "Only items in this status.",
                        },
                        "page": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                            "default": 1,
                            "description": "Page number.",
                        },
                        "name": {"type": "string", "minLength": 1, "description": "Name."},
                        "kind": {"$ref": "#/$defs/Kind", "description": "Kind."},
                    },
                    "required": ["name", "kind"],
                    "$defs": {"Kind": {"enum": ["a", "b"], "type": "string"}},
                },
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "state": {"type": "string", "enum": ["done", "queued"]},
                        "count": {"type": "integer", "minimum": 0, "maximum": 10},
                    },
                    "required": ["state", "count"],
                },
                "annotations": {"readOnlyHint": True},
            },
            "other": {
                "description": "Another tool.",
                "input_schema": {"type": "object", "properties": {}},
                "output_schema": None,
                "annotations": None,
            },
        },
        "public": {
            "__all__": ["Thing", "create"],
            "signatures": {
                "create": {
                    "async": False,
                    "parameters": [
                        {"name": "a", "kind": "positional-or-keyword", "annotation": "str"},
                        {
                            "name": "b",
                            "kind": "positional-or-keyword",
                            "annotation": "int",
                            "default": "1",
                        },
                        {
                            "name": "c",
                            "kind": "keyword-only",
                            "annotation": "bool",
                            "default": "False",
                        },
                    ],
                    "return": "Thing",
                },
                "Thing.close": {"async": True, "parameters": [], "return": "None"},
            },
        },
    }


def tool(snapshot: dict[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = snapshot["tools"]["list_items"]
    return found


def argument(snapshot: dict[str, Any], name: str) -> dict[str, Any]:
    found: dict[str, Any] = tool(snapshot)["input_schema"]["properties"][name]
    return found


def result(snapshot: dict[str, Any], name: str) -> dict[str, Any]:
    found: dict[str, Any] = tool(snapshot)["output_schema"]["properties"][name]
    return found


def parameters(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = snapshot["public"]["signatures"]["create"]["parameters"]
    return found


def lines_after(edit: Callable[[dict[str, Any]], None]) -> list[str]:
    head = base()
    edit(head)
    return [str(change) for change in diff(base(), head)]


def set_in(
    target: Callable[[dict[str, Any]], dict[str, Any]], **values: object
) -> Callable[[dict[str, Any]], None]:
    def edit(snapshot: dict[str, Any]) -> None:
        target(snapshot).update(values)

    return edit


def drop(
    target: Callable[[dict[str, Any]], dict[str, Any]], key: str
) -> Callable[[dict[str, Any]], None]:
    def edit(snapshot: dict[str, Any]) -> None:
        del target(snapshot)[key]

    return edit


def status(snapshot: dict[str, Any]) -> dict[str, Any]:
    return argument(snapshot, "status")


def status_text(snapshot: dict[str, Any]) -> dict[str, Any]:
    branch: dict[str, Any] = status(snapshot)["anyOf"][0]
    return branch


def page(snapshot: dict[str, Any]) -> dict[str, Any]:
    return argument(snapshot, "page")


def name(snapshot: dict[str, Any]) -> dict[str, Any]:
    return argument(snapshot, "name")


def kind_def(snapshot: dict[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = tool(snapshot)["input_schema"]["$defs"]["Kind"]
    return found


def state(snapshot: dict[str, Any]) -> dict[str, Any]:
    return result(snapshot, "state")


def count(snapshot: dict[str, Any]) -> dict[str, Any]:
    return result(snapshot, "count")


def input_schema(snapshot: dict[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = tool(snapshot)["input_schema"]
    return found


def output_schema(snapshot: dict[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = tool(snapshot)["output_schema"]
    return found


def signature(snapshot: dict[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = snapshot["public"]["signatures"]["create"]
    return found


def parameter(index: int) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def target(snapshot: dict[str, Any]) -> dict[str, Any]:
        return parameters(snapshot)[index]

    return target


def remove_tool(snapshot: dict[str, Any]) -> None:
    del snapshot["tools"]["other"]


def add_tool(snapshot: dict[str, Any]) -> None:
    snapshot["tools"]["new_tool"] = copy.deepcopy(snapshot["tools"]["other"])


def add_argument(*, required: bool) -> Callable[[dict[str, Any]], None]:
    def edit(snapshot: dict[str, Any]) -> None:
        input_schema(snapshot)["properties"]["extra"] = {"type": "string", "description": "x"}
        if required:
            input_schema(snapshot)["required"].append("extra")

    return edit


def require(field: str) -> Callable[[dict[str, Any]], None]:
    def edit(snapshot: dict[str, Any]) -> None:
        input_schema(snapshot)["required"].append(field)

    return edit


def unrequire(
    schema: Callable[[dict[str, Any]], dict[str, Any]], field: str
) -> Callable[[dict[str, Any]], None]:
    def edit(snapshot: dict[str, Any]) -> None:
        schema(snapshot)["required"].remove(field)

    return edit


def remove_property(
    schema: Callable[[dict[str, Any]], dict[str, Any]], field: str
) -> Callable[[dict[str, Any]], None]:
    def edit(snapshot: dict[str, Any]) -> None:
        del schema(snapshot)["properties"][field]
        if field in schema(snapshot).get("required", []):
            schema(snapshot)["required"].remove(field)

    return edit


def not_nullable(snapshot: dict[str, Any]) -> None:
    argument(snapshot, "status").update(status_text(snapshot))
    del argument(snapshot, "status")["anyOf"]


def nullable(snapshot: dict[str, Any]) -> None:
    text = dict(name(snapshot))
    del text["description"]
    name(snapshot).clear()
    name(snapshot).update({"anyOf": [text, {"type": "null"}], "description": "Name."})


def add_result_field(snapshot: dict[str, Any]) -> None:
    output_schema(snapshot)["properties"]["extra"] = {"type": "string"}


def remove_output_schema(snapshot: dict[str, Any]) -> None:
    tool(snapshot)["output_schema"] = None


def add_output_schema(snapshot: dict[str, Any]) -> None:
    snapshot["tools"]["other"]["output_schema"] = {"type": "object"}


def remove_name(snapshot: dict[str, Any]) -> None:
    snapshot["public"]["__all__"].remove("Thing")


def add_name(snapshot: dict[str, Any]) -> None:
    snapshot["public"]["__all__"].append("helper")


def remove_signature(snapshot: dict[str, Any]) -> None:
    del snapshot["public"]["signatures"]["Thing.close"]


def add_signature(snapshot: dict[str, Any]) -> None:
    snapshot["public"]["signatures"]["Thing.open"] = {"async": True, "parameters": []}


def remove_parameter(snapshot: dict[str, Any]) -> None:
    del parameters(snapshot)[2]


def add_parameter(entry: dict[str, str], position: int = 3) -> Callable[[dict[str, Any]], None]:
    def edit(snapshot: dict[str, Any]) -> None:
        parameters(snapshot).insert(position, entry)

    return edit


def swap_positional(snapshot: dict[str, Any]) -> None:
    listed = parameters(snapshot)
    listed[0], listed[1] = listed[1], listed[0]


def rename_title(snapshot: dict[str, Any]) -> None:
    input_schema(snapshot)["title"] = "renamedArguments"


BREAKING = True
NON_BREAKING = False

RULES: list[tuple[str, Callable[[dict[str, Any]], None], bool, str]] = [
    # tools
    ("tool removed", remove_tool, BREAKING, "tool other: removed"),
    ("tool added", add_tool, NON_BREAKING, "tool new_tool: added"),
    (
        "description changed",
        set_in(tool, description="List items."),
        NON_BREAKING,
        "description changed",
    ),
    (
        "annotation changed",
        set_in(lambda s: tool(s)["annotations"], readOnlyHint=False),
        NON_BREAKING,
        "annotation readOnlyHint changed (true -> false)",
    ),
    ("argument removed", remove_property(input_schema, "page"), BREAKING, '"page" removed'),
    (
        "optional argument added",
        add_argument(required=False),
        NON_BREAKING,
        '"extra" added (optional)',
    ),
    ("required argument added", add_argument(required=True), BREAKING, '"extra" added (required)'),
    ("argument newly required", require("page"), BREAKING, '"page" now required'),
    (
        "argument now optional",
        unrequire(input_schema, "name"),
        NON_BREAKING,
        '"name" now optional',
    ),
    (
        "argument type changed",
        set_in(page, type="string"),
        BREAKING,
        '"page" type changed from integer to string',
    ),
    ("argument no longer nullable", not_nullable, BREAKING, '"status" no longer allows null'),
    ("argument now nullable", nullable, NON_BREAKING, '"name" now allows null'),
    (
        "enum narrowed",
        set_in(status_text, enum=["open"]),
        BREAKING,
        '"status" values removed: "closed"',
    ),
    (
        "enum widened",
        set_in(status_text, enum=["open", "closed", "held"]),
        NON_BREAKING,
        '"status" values added: "held"',
    ),
    (
        "enum added",
        set_in(name, enum=["x"]),
        BREAKING,
        '"name" now limited to "x"',
    ),
    ("enum removed", drop(status_text, "enum"), NON_BREAKING, '"status" no longer limited'),
    (
        "enum narrowed through a $ref",
        set_in(kind_def, enum=["a"]),
        BREAKING,
        '"kind" values removed: "b"',
    ),
    (
        "minLength added",
        set_in(status_text, minLength=1),
        BREAKING,
        '"status" minLength tightened (none -> 1)',
    ),
    ("minLength removed", drop(name, "minLength"), NON_BREAKING, "minLength loosened (1 -> none)"),
    ("minLength raised", set_in(name, minLength=3), BREAKING, "minLength tightened (1 -> 3)"),
    ("minimum lowered", set_in(page, minimum=0), NON_BREAKING, "minimum loosened (1 -> 0)"),
    ("maximum lowered", set_in(page, maximum=50), BREAKING, "maximum tightened (100 -> 50)"),
    ("maximum raised", set_in(page, maximum=500), NON_BREAKING, "maximum loosened (100 -> 500)"),
    (
        "pattern added",
        set_in(name, pattern="^[a-z]+$"),
        BREAKING,
        '"name" pattern tightened',
    ),
    ("format added", set_in(name, format="uuid"), BREAKING, '"name" format tightened'),
    (
        "additionalProperties false added",
        set_in(input_schema, additionalProperties=False),
        BREAKING,
        "arguments additionalProperties tightened",
    ),
    (
        "argument description changed",
        set_in(page, description="The page."),
        NON_BREAKING,
        '"page" description changed',
    ),
    ("argument default changed", set_in(page, default=2), NON_BREAKING, '"page" default changed'),
    ("schema title changed", rename_title, NON_BREAKING, "arguments title changed"),
    # results
    ("result field removed", remove_property(output_schema, "count"), BREAKING, '"count" removed'),
    ("result field added", add_result_field, NON_BREAKING, 'result field "extra" added'),
    (
        "result field now optional",
        unrequire(output_schema, "count"),
        BREAKING,
        '"count" now optional',
    ),
    ("output schema removed", remove_output_schema, BREAKING, "output schema removed"),
    ("output schema added", add_output_schema, NON_BREAKING, "output schema added"),
    (
        "result type widened",
        set_in(count, type=["integer", "null"]),
        BREAKING,
        '"count" now allows null',
    ),
    (
        "result enum widened",
        set_in(state, enum=["done", "queued", "failed"]),
        BREAKING,
        '"state" values added: "failed"',
    ),
    (
        "result enum narrowed",
        set_in(state, enum=["done"]),
        NON_BREAKING,
        '"state" values removed: "queued"',
    ),
    (
        "result bound loosened",
        set_in(count, maximum=1000),
        BREAKING,
        '"count" maximum loosened (10 -> 1000)',
    ),
    (
        "result bound tightened",
        set_in(count, maximum=5),
        NON_BREAKING,
        '"count" maximum tightened (10 -> 5)',
    ),
    # the public API
    ("public name removed", remove_name, BREAKING, "permit_mcp.__all__: Thing removed"),
    ("public name added", add_name, NON_BREAKING, "permit_mcp.__all__: helper added"),
    ("signature removed", remove_signature, BREAKING, "signature Thing.close: removed"),
    ("signature added", add_signature, NON_BREAKING, "signature Thing.open: added"),
    ("parameter removed", remove_parameter, BREAKING, 'parameter "c" removed'),
    (
        "optional parameter added",
        add_parameter({"name": "d", "kind": "keyword-only", "annotation": "int", "default": "0"}),
        NON_BREAKING,
        'parameter "d" added (optional)',
    ),
    (
        "required parameter added",
        add_parameter({"name": "d", "kind": "keyword-only", "annotation": "int"}),
        BREAKING,
        'parameter "d" added (required)',
    ),
    (
        "var-keyword parameter added",
        add_parameter({"name": "rest", "kind": "var-keyword", "annotation": "str"}),
        NON_BREAKING,
        'parameter "rest" added (optional)',
    ),
    (
        "optional positional parameter inserted",
        add_parameter(
            {"name": "z", "kind": "positional-or-keyword", "annotation": "int", "default": "0"},
            position=0,
        ),
        BREAKING,
        "positional parameters changed from (a, b) to (z, a, b)",
    ),
    (
        "positional parameters reordered",
        swap_positional,
        BREAKING,
        "positional parameters changed from (a, b) to (b, a)",
    ),
    (
        "parameter made keyword-only",
        set_in(parameter(1), kind="keyword-only"),
        BREAKING,
        'parameter "b" changed from positional-or-keyword to keyword-only',
    ),
    (
        "keyword-only parameter made positional-or-keyword",
        set_in(parameter(2), kind="positional-or-keyword"),
        NON_BREAKING,
        'parameter "c" changed from keyword-only to positional-or-keyword',
    ),
    (
        "parameter type changed",
        set_in(parameter(0), annotation="bytes"),
        BREAKING,
        'parameter "a" type changed from str to bytes',
    ),
    ("default removed", drop(parameter(1), "default"), BREAKING, '"b" is now required'),
    ("default added", set_in(parameter(0), default="''"), NON_BREAKING, '"a" is now optional'),
    (
        "default changed",
        set_in(parameter(1), default="2"),
        NON_BREAKING,
        '"b" default changed from 1 to 2',
    ),
    (
        "return type changed",
        set_in(signature, **{"return": "None"}),
        BREAKING,
        "return type changed from Thing to None",
    ),
    ("made async", set_in(signature, **{"async": True}), BREAKING, "changed from sync to async"),
    (
        "made sync",
        set_in(lambda s: s["public"]["signatures"]["Thing.close"], **{"async": False}),
        BREAKING,
        "changed from async to sync",
    ),
]


@pytest.mark.parametrize(
    ("edit", "breaking", "expected"),
    [rule[1:] for rule in RULES],
    ids=[rule[0] for rule in RULES],
)
def test_each_rule_is_labelled(
    edit: Callable[[dict[str, Any]], None], *, breaking: bool, expected: str
) -> None:
    lines = lines_after(edit)
    label = "BREAKING" if breaking else "non-breaking"
    matching = [line for line in lines if expected in line]
    assert matching, lines
    assert all(line.startswith(f"{label:<12}  ") for line in matching), lines
    assert any(line.startswith("BREAKING") for line in lines) is breaking, lines


def test_an_unchanged_snapshot_has_no_change() -> None:
    assert diff(base(), base()) == []


def run(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True, check=False
    )


def write(tmp_path: Path, name: str, snapshot: object) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    return path


def committed() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(COMMITTED.read_text(encoding="utf-8"))
    return loaded


def test_the_committed_snapshot_against_itself_exits_0() -> None:
    completed = run(COMMITTED, COMMITTED)
    assert completed.returncode == 0, completed.stdout
    assert completed.stdout == "surface_diff: 0 changes, 0 breaking\n"


def test_a_non_breaking_change_exits_0(tmp_path: Path) -> None:
    head = committed()
    head["tools"]["check_permission"]["description"] = "Changed."
    completed = run(COMMITTED, write(tmp_path, "head.json", head))
    assert completed.returncode == 0, completed.stdout
    assert completed.stdout.splitlines() == [
        "non-breaking  tool check_permission: description changed",
        "surface_diff: 1 changes, 0 breaking",
    ]


def test_a_breaking_change_exits_1(tmp_path: Path) -> None:
    head = committed()
    del head["tools"]["deny_access_request"]
    head["tools"]["list_access_requests"]["input_schema"]["properties"]["per_page"]["maximum"] = 50
    completed = run(COMMITTED, write(tmp_path, "head.json", head))
    assert completed.returncode == 1, completed.stdout
    assert completed.stdout.splitlines() == [
        "BREAKING      tool deny_access_request: removed",
        (
            'BREAKING      tool list_access_requests: argument "per_page" maximum tightened '
            "(100 -> 50)"
        ),
        "surface_diff: 2 changes, 2 breaking",
    ]


def test_a_new_validator_on_a_real_argument_is_breaking(tmp_path: Path) -> None:
    head = committed()
    reason = head["tools"]["create_access_request"]["input_schema"]["properties"]["reason"]
    reason["maxLength"] = 500
    completed = run(COMMITTED, write(tmp_path, "head.json", head))
    assert completed.returncode == 1, completed.stdout
    assert 'argument "reason" maxLength tightened (none -> 500)' in completed.stdout


def test_a_renamed_result_field_on_a_real_tool_is_breaking(tmp_path: Path) -> None:
    head = committed()
    output = head["tools"]["check_permission"]["output_schema"]
    output["properties"]["permitted"] = output["properties"].pop("allowed")
    output["required"] = ["permitted"]
    completed = run(COMMITTED, write(tmp_path, "head.json", head))
    assert completed.returncode == 1, completed.stdout
    assert 'BREAKING      tool check_permission: result field "allowed" removed' in completed.stdout


def test_a_dropped_result_field_on_a_real_tool_is_breaking(tmp_path: Path) -> None:
    head = committed()
    output = head["tools"]["cancel_operation_approval"]["output_schema"]
    del output["properties"]["operation_approval"]
    output["required"].remove("operation_approval")
    completed = run(COMMITTED, write(tmp_path, "head.json", head))
    assert completed.returncode == 1, completed.stdout
    assert 'result field "operation_approval" removed' in completed.stdout


def nested_case(direction: str, before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """The changes when a property of list_items' arguments or result goes from before to after."""
    old, new = base(), base()
    schema = input_schema if direction == "input" else output_schema
    schema(old)["properties"]["nested"] = before
    schema(new)["properties"]["nested"] = after
    return [str(change) for change in diff(old, new)]


NESTED: list[tuple[str, str, dict[str, Any], dict[str, Any], bool, str]] = [
    (
        "result values boolean to string",
        "output",
        {"type": "object", "additionalProperties": {"type": "boolean"}},
        {"type": "object", "additionalProperties": {"type": "string"}},
        BREAKING,
        '"nested" property values type changed from boolean to string',
    ),
    (
        "argument values loosened",
        "input",
        {"type": "object", "additionalProperties": {"type": "string", "maxLength": 5}},
        {"type": "object", "additionalProperties": {"type": "string"}},
        NON_BREAKING,
        '"nested" property values maxLength loosened',
    ),
    (
        "argument items string to integer enum",
        "input",
        {"type": "array", "items": {"type": "string"}},
        {"type": "array", "items": {"type": "integer", "enum": [1, 2]}},
        BREAKING,
        '"nested" items type changed from string to integer',
    ),
    (
        "result items enum narrowed",
        "output",
        {"type": "array", "items": {"enum": ["a", "b"], "type": "string"}},
        {"type": "array", "items": {"enum": ["a"], "type": "string"}},
        NON_BREAKING,
        '"nested" items values removed: "b"',
    ),
    (
        "untyped argument becomes string",
        "input",
        {"description": "Anything."},
        {"type": "string", "description": "Anything."},
        BREAKING,
        '"nested" now limited to type string',
    ),
    (
        "untyped result becomes string",
        "output",
        {"description": "Anything."},
        {"type": "string", "description": "Anything."},
        NON_BREAKING,
        '"nested" now limited to type string',
    ),
    (
        "typed argument becomes untyped",
        "input",
        {"type": "string"},
        {},
        NON_BREAKING,
        '"nested" now allows any type',
    ),
    (
        "argument items untyped become integers",
        "input",
        {"type": "array"},
        {"type": "array", "items": {"type": "integer"}},
        BREAKING,
        '"nested" items now limited to type integer',
    ),
]


@pytest.mark.parametrize(
    ("direction", "before", "after", "breaking", "expected"),
    [case[1:] for case in NESTED],
    ids=[case[0] for case in NESTED],
)
def test_items_additional_properties_and_untyped_schemas_are_compared(
    direction: str,
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    breaking: bool,
    expected: str,
) -> None:
    lines = nested_case(direction, before, after)
    label = "BREAKING" if breaking else "non-breaking"
    matching = [line for line in lines if expected in line]
    assert matching, lines
    assert all(line.startswith(f"{label:<12}  ") for line in matching), lines
    assert any(line.startswith("BREAKING") for line in lines) is breaking, lines


@pytest.mark.parametrize(
    "args",
    [(), ("only-one",), ("a", "b", "c")],
    ids=["none", "one", "three"],
)
def test_exits_2_on_wrong_arguments(args: tuple[str, ...]) -> None:
    completed = run(*args)
    assert completed.returncode == 2
    assert "DID NOT RUN: usage" in completed.stdout


def test_exits_2_on_a_missing_file(tmp_path: Path) -> None:
    completed = run(COMMITTED, tmp_path / "absent.json")
    assert completed.returncode == 2
    assert "DID NOT RUN: head" in completed.stdout


def test_exits_2_on_a_file_that_is_not_json(tmp_path: Path) -> None:
    path = tmp_path / "base.json"
    path.write_text("{not json", encoding="utf-8")
    completed = run(path, COMMITTED)
    assert completed.returncode == 2
    assert "DID NOT RUN: base" in completed.stdout


def not_snapshots() -> list[tuple[str, object]]:
    no_tools = committed()
    no_tools["tools"] = {}
    tool_without_schema = committed()
    del tool_without_schema["tools"]["check_permission"]["input_schema"]
    no_public = committed()
    del no_public["public"]
    bad_parameter = committed()
    bad_parameter["public"]["signatures"]["bound_user"]["parameters"] = [{"name": "x"}]
    return [
        ("a list", []),
        ("no tools", no_tools),
        ("a tool without an input schema", tool_without_schema),
        ("no public API", no_public),
        ("a parameter without a kind", bad_parameter),
    ]


@pytest.mark.parametrize(
    "snapshot", [case[1] for case in not_snapshots()], ids=[case[0] for case in not_snapshots()]
)
def test_exits_2_on_a_file_that_is_not_a_snapshot(tmp_path: Path, snapshot: object) -> None:
    completed = run(COMMITTED, write(tmp_path, "head.json", snapshot))
    assert completed.returncode == 2
    assert "DID NOT RUN: head" in completed.stdout
