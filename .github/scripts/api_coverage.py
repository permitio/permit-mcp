#!/usr/bin/env python3
"""Report which Permit API and PDP operations the server's tests exercise.

Usage:
  api_coverage.py report --spec control-plane=PATH --spec pdp=PATH --allowlist PATH
                         --record PATH --origins PATH [--baseline control-plane=PATH]
                         [--e2e-record PATH --e2e-origins PATH] [--summary PATH]
  api_coverage.py snapshot API SPEC --allowlist PATH --source URL [--fetched DATE]
                           [--out-dir DIR]
  api_coverage.py compare API SPEC --inventory PATH --allowlist PATH

Where the numbers come from:

* The allowlist (.github/scripts/api_coverage_allowlist.json) sets the scope of each API:
  every operation of the tags it names, and the operations it names that the server
  calls outside those tags. Everything else is out of scope, for the one reason the
  allowlist gives. It also gives a reason for each in-scope operation no test sends, and
  for each request that matches no in-scope operation (sdk_only).
* The in-scope operations are in two inventories under .github/api-specs/: the control
  plane's, taken from https://api.permit.io/v2/openapi.json, and the PDP's. For each
  operation an inventory keeps its method, path template, operationId, tags, deprecated
  flag, parameters (name, in, required) and request body schema, which is what the
  drift check compares. `snapshot` writes an inventory from a downloaded OpenAPI
  document, with a .source.json file beside it that says where and when.
* What the server sends comes from a record of the requests the offline tests sent
  (tests/api_record.py writes it when PERMIT_MCP_API_RECORD is set): one JSON line per
  request with its method, origin, raw path, status and test id. The origins file beside
  it maps each test server's origin to the APIs it serves. A request is matched only
  against the inventories of its origin's APIs, to the path template of its method with
  the most literal segments. A request that got no response (status null) counts for
  nothing.

Each in-scope operation is covered (a test got a response from it), untested (the server
calls it but no test sent it) or missing (no tool calls it). The last two come from the
allowlist.

Each in-scope operation also has a stage: EAP when one of its tags ends in "(EAP)",
otherwise deprecated when the spec marks it so, otherwise GA. The report counts covered,
untested and missing operations per API and stage.

With --e2e-record and --e2e-origins, a record and origins file the end-to-end suite wrote
in the same format, the report also says, for each in-scope operation, whether it was
exercised end to end: whether a request of that record, matched as above, got a 2xx or
3xx answer from it. An error answer or no answer does not count. Without them the column
says "not run". The end-to-end record only fills the column: it never makes a finding.

The report fails (exit 1) on a finding:

* an in-scope operation no test sent, with no allowlist entry or an entry with no reason;
* a request that matches no in-scope operation of its origin's APIs and no sdk_only
  entry with a reason: add it to the scope or to sdk_only;
* a stale allowlist entry: an entry for an operation that is covered or not in the
  inventory, an sdk_only entry no request matches, a scope tag or operation that is not
  in the inventory or is listed twice, or an inventory operation the scope leaves out;
* with --baseline, an in-scope operation added, removed or changed since the baseline.

Exit 2 means the report did not run, and is never a clean result: an inventory that
cannot be read, is malformed or is empty, an allowlist that cannot be read or is
malformed, a record that is missing, malformed or holds fewer requests than the
minimum, an origins file that names no origin for an API, an end-to-end record or origins
file that is malformed or empty (or one given without the other), or any other error. The
Markdown report goes to --summary (appended) or to stdout; each finding is also printed
as a GitHub error annotation.

`snapshot` exits 0 when it wrote the inventory, and 2 when the document cannot be read
or lists fewer operations than its minimum, or the allowlist cannot be read.

`compare` checks a downloaded OpenAPI document, such as the one a running PDP publishes,
against a committed inventory: it takes the document's in-scope operations as `snapshot`
would, and compares every field the inventory keeps, as the drift check does. It exits 0
when they match, 1 when they differ, printing each difference and a unified diff of the
two inventories, and 2 when the document, the inventory or the allowlist cannot be used.

Stdlib only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import re
import sys
import traceback
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any

PASS = 0
FINDINGS = 1
DID_NOT_RUN = 2

CONTROL_PLANE = "control-plane"
PDP = "pdp"
APIS = (CONTROL_PLANE, PDP)
TITLES = {CONTROL_PLANE: "Control plane", PDP: "PDP"}

COVERED = "covered"
UNTESTED = "untested"
MISSING = "missing"
ENTRY_STATUSES = (UNTESTED, MISSING)

GA = "GA"
EAP = "EAP"
DEPRECATED = "deprecated"
STAGES = (GA, EAP, DEPRECATED)
EAP_TAG_SUFFIX = "(EAP)"
# What the end-to-end column says for an operation, and for every one without a record.
EXERCISED = "yes"
NOT_EXERCISED = "no"
NOT_RUN = "not run"

# The fewest operations a downloaded spec must list, and the fewest requests a record
# must hold: far below today's counts (263 control-plane operations, 1 PDP operation,
# about 450 recorded requests), so they trip only on a truncated spec or a recorder that
# stopped seeing requests.
MIN_OPERATIONS = {CONTROL_PLANE: 200, PDP: 1}
MIN_REQUESTS = 200
# An end-to-end record with no request means the suite did not run.
MIN_E2E_REQUESTS = 1

HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
PARAMETER = re.compile(r"\{[^/{}]*\}")
OUTSIDE_A_TEST = "(outside any test)"
INVENTORY_FIELDS = {
    "method",
    "path",
    "operationId",
    "tags",
    "deprecated",
    "parameters",
    "requestBody",
}
RECORD_FIELDS = {"method", "origin", "path", "status", "test"}


class CoverageError(Exception):
    """An input could not be used, so the report did not run (exit 2)."""


def read_json(path: Path, what: str) -> object:
    """Return the JSON document at `path`.

    Raises:
        CoverageError: The file cannot be read or is not JSON.

    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        msg = f"could not read {what} {path}: {exc}"
        raise CoverageError(msg) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        msg = f"{what} {path} is not JSON: {exc}"
        raise CoverageError(msg) from exc


# --- inventories -----------------------------------------------------------------


def normalize(path: str) -> str:
    """Drop a path template's parameter names: `/users/{user_id}` is `/users/{}`."""
    return PARAMETER.sub("{}", path)


def template_pattern(path: str) -> re.Pattern[str]:
    """Match the raw paths of a template; a parameter is one non-empty segment."""
    parts = PARAMETER.split(path)
    return re.compile("[^/]+".join(re.escape(part) for part in parts) + r"\Z")


def specificity(path: str) -> tuple[int, tuple[int, ...]]:
    """Rank a template: more literal segments first, then literal segments to the left."""
    literal = tuple(0 if PARAMETER.fullmatch(part) else 1 for part in path.split("/"))
    return sum(literal), literal


@dataclass(frozen=True)
class Operation:
    """One operation of an inventory."""

    api: str
    method: str
    path: str
    operation_id: str
    tags: tuple[str, ...]
    deprecated: bool
    parameters: tuple[tuple[str, str, bool], ...]
    request_body: str | None
    pattern: re.Pattern[str] = field(compare=False, repr=False)

    @property
    def name(self) -> str:
        """The operation as the report and the allowlist write it: `GET /v2/...`."""
        return f"{self.method} {self.path}"

    @property
    def key(self) -> tuple[str, str, str]:
        """The operation's identity: API, method, and path without parameter names."""
        return (self.api, self.method, normalize(self.path))

    @property
    def stage(self) -> str:
        """EAP when a tag ends in "(EAP)", else deprecated when the spec says so, else GA."""
        if any(tag.endswith(EAP_TAG_SUFFIX) for tag in self.tags):
            return EAP
        if self.deprecated:
            return DEPRECATED
        return GA


@dataclass
class Inventory:
    """The in-scope operations of one API, and where they came from."""

    api: str
    source: str
    operations: list[Operation]

    def match(self, method: str, path: str) -> Operation | None:
        """Return the operation a request belongs to: the most literal matching template."""
        candidates = [
            op for op in self.operations if op.method == method and op.pattern.match(path)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda op: (specificity(op.path), op.path))


def _resolve(document: dict[str, Any], value: object) -> object:
    """Follow a local `$ref` ("#/components/...") in `document`, once."""
    if not isinstance(value, dict) or not isinstance(value.get("$ref"), str):
        return value
    target: object = document
    for part in value["$ref"].removeprefix("#/").split("/"):
        target = target.get(part) if isinstance(target, dict) else None
    if target is None:
        msg = f"the spec has no {value['$ref']}"
        raise CoverageError(msg)
    return target


def _parameters(document: dict[str, Any], raw: object) -> list[dict[str, Any]]:
    parameters = []
    for item in raw if isinstance(raw, list) else []:
        parameter = _resolve(document, item)
        if isinstance(parameter, dict):
            parameters.append(
                {
                    "in": str(parameter.get("in")),
                    "name": str(parameter.get("name")),
                    "required": parameter.get("required") is True,
                }
            )
    return sorted(parameters, key=lambda p: (p["in"], p["name"]))


def _request_body(document: dict[str, Any], raw: object) -> str | None:
    """Name a request body by its schema: the schema's `$ref`, else its title or type."""
    body = _resolve(document, raw)
    if not isinstance(body, dict):
        return None
    content = body.get("content")
    media = next(iter(content.values()), None) if isinstance(content, dict) else None
    schema = media.get("schema") if isinstance(media, dict) else None
    if not isinstance(schema, dict):
        return "no schema"
    return str(schema.get("$ref") or schema.get("title") or schema.get("type") or "inline")


def operations_of(document: object, label: str) -> list[dict[str, Any]]:
    """List every operation of an OpenAPI document as an inventory entry, sorted.

    Raises:
        CoverageError: The document has no `paths` object, or a `$ref` it uses is missing.

    """
    paths = document.get("paths") if isinstance(document, dict) else None
    if not isinstance(document, dict) or not isinstance(paths, dict):
        msg = f"{label} has no `paths` object"
        raise CoverageError(msg)
    entries: list[dict[str, Any]] = []
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters")
        for method in HTTP_METHODS:
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue
            own = operation.get("parameters")
            raw = [
                *(shared if isinstance(shared, list) else []),
                *(own if isinstance(own, list) else []),
            ]
            entries.append(
                {
                    "method": method.upper(),
                    "path": str(path),
                    "operationId": str(operation.get("operationId") or ""),
                    "tags": [str(tag) for tag in operation.get("tags") or []],
                    "deprecated": operation.get("deprecated") is True,
                    "parameters": _parameters(document, raw),
                    "requestBody": _request_body(document, operation.get("requestBody")),
                }
            )
    return sorted(entries, key=lambda entry: (entry["path"], entry["method"]))


def _operation(api: str, raw: object, where: str) -> Operation:
    if not isinstance(raw, dict) or set(raw) != INVENTORY_FIELDS:
        msg = f"{where} needs exactly {', '.join(sorted(INVENTORY_FIELDS))}"
        raise CoverageError(msg)
    method, path, operation_id = raw["method"], raw["path"], raw["operationId"]
    tags, deprecated, body = raw["tags"], raw["deprecated"], raw["requestBody"]
    parameters = raw["parameters"]
    if (
        method not in {m.upper() for m in HTTP_METHODS}
        or not isinstance(path, str)
        or not path.startswith("/")
        or not isinstance(operation_id, str)
        or not isinstance(tags, list)
        or not all(isinstance(tag, str) for tag in tags)
        or not isinstance(deprecated, bool)
        or not (body is None or isinstance(body, str))
        or not isinstance(parameters, list)
        or not all(
            isinstance(p, dict)
            and set(p) == {"name", "in", "required"}
            and isinstance(p["name"], str)
            and isinstance(p["in"], str)
            and isinstance(p["required"], bool)
            for p in parameters
        )
    ):
        msg = f"{where} has a field of the wrong type"
        raise CoverageError(msg)
    return Operation(
        api=api,
        method=str(method),
        path=path,
        operation_id=operation_id,
        tags=tuple(tags),
        deprecated=deprecated,
        parameters=tuple((p["in"], p["name"], p["required"]) for p in parameters),
        request_body=body,
        pattern=template_pattern(path),
    )


def load_inventory(api: str, path: Path) -> Inventory:
    """Read an inventory that `snapshot` wrote.

    Raises:
        CoverageError: The inventory or its source file cannot be read or is malformed,
            two operations are the same, or it lists no operation.

    """
    label = f"the {TITLES[api]} inventory"
    document = read_json(path, label)
    raw = document.get("operations") if isinstance(document, dict) else None
    if not isinstance(raw, list):
        msg = f"{label} {path} has no `operations` list"
        raise CoverageError(msg)
    operations: list[Operation] = []
    seen: set[tuple[str, str, str]] = set()
    for index, item in enumerate(raw):
        operation = _operation(api, item, f"operation {index} of {label} {path}")
        if operation.key in seen:
            msg = f"{label} {path} lists {operation.name} twice"
            raise CoverageError(msg)
        seen.add(operation.key)
        operations.append(operation)
    if not operations:
        msg = f"{label} {path} lists no operation"
        raise CoverageError(msg)
    return Inventory(api=api, source=_describe(path, len(operations)), operations=operations)


def source_file(inventory: Path) -> Path:
    """Return the file beside an inventory that says where and when it was taken."""
    return inventory.with_name(inventory.name.removesuffix(".json") + ".source.json")


def _describe(path: Path, count: int) -> str:
    source = read_json(source_file(path), "the source file")
    if not isinstance(source, dict) or not all(
        isinstance(source.get(key), str) and source[key] for key in ("source", "fetched")
    ):
        msg = f'the source file {source_file(path)} needs a "source" and a "fetched" string'
        raise CoverageError(msg)
    return (
        f"{_code(path)}, {_plural(count, 'in-scope operation')}, taken from "
        f"{_cell(source['source'])} on {_cell(source['fetched'])}"
    )


# --- the request record ------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    """One recorded request."""

    method: str
    origin: str
    path: str
    status: int | None
    test: str

    @property
    def name(self) -> str:
        """The request as the report writes it."""
        return f"{self.method} {self.path}"


def load_record(path: Path) -> list[Request]:
    """Read the request record tests/api_record.py wrote during the offline tests.

    Raises:
        CoverageError: The record is missing, malformed, or holds fewer requests than the
            minimum.

    """
    return _read_requests(path, "the request record", MIN_REQUESTS)


def load_e2e_record(path: Path) -> list[Request]:
    """Read the request record tests/api_record.py wrote during the end-to-end suite.

    Raises:
        CoverageError: The record is missing, malformed, or holds no request.

    """
    return _read_requests(path, "the end-to-end record", MIN_E2E_REQUESTS)


def _read_requests(path: Path, label: str, minimum: int) -> list[Request]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        msg = f"could not read {label} {path}: {exc}"
        raise CoverageError(msg) from exc
    except UnicodeDecodeError as exc:
        msg = f"{label} {path} is not UTF-8 text: {exc}"
        raise CoverageError(msg) from exc
    requests = [
        _request(f"line {number} of {label} {path}", text)
        for number, text in enumerate(lines, 1)
        if text
    ]
    if len(requests) < minimum:
        msg = (
            f"{label} {path} holds {len(requests)} requests, fewer than the minimum of "
            f"{minimum}; the recorder missed requests or the tests did not run"
        )
        raise CoverageError(msg)
    return requests


def _request(where: str, text: str) -> Request:
    try:
        line = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"{where} is not JSON: {exc}"
        raise CoverageError(msg) from exc
    if not isinstance(line, dict) or set(line) != RECORD_FIELDS:
        msg = f"{where} is not a request line of {', '.join(sorted(RECORD_FIELDS))}"
        raise CoverageError(msg)
    method, origin, request_path = line["method"], line["origin"], line["path"]
    status, test = line["status"], line["test"]
    if (
        not isinstance(method, str)
        or not isinstance(origin, str)
        or not isinstance(request_path, str)
        or not request_path.startswith("/")
        or not (status is None or (isinstance(status, int) and not isinstance(status, bool)))
        or not (test is None or isinstance(test, str))
    ):
        msg = f"{where} is not a well-formed request"
        raise CoverageError(msg)
    return Request(method.upper(), origin, request_path, status, test or OUTSIDE_A_TEST)


def load_origins(path: Path, label: str = "the origins file") -> dict[str, tuple[str, ...]]:
    """Read an origins file: each server's origin and the APIs it serves.

    Raises:
        CoverageError: The file cannot be read, is malformed, names an unknown API, or
            names no origin for an API.

    """
    document = read_json(path, label)
    if not isinstance(document, dict) or not all(
        isinstance(apis, list) and apis and set(apis) <= set(APIS) for apis in document.values()
    ):
        msg = f"{label} {path} must map each origin to a list of {', '.join(APIS)}"
        raise CoverageError(msg)
    origins = {str(origin): tuple(apis) for origin, apis in document.items()}
    for api in APIS:
        if not any(api in apis for apis in origins.values()):
            msg = f"{label} {path} names no origin for the {TITLES[api]}"
            raise CoverageError(msg)
    return origins


@dataclass(frozen=True)
class EndToEnd:
    """What the end-to-end suite sent, and the APIs each of its origins serves."""

    requests: list[Request]
    origins: dict[str, tuple[str, ...]]

    @property
    def answered(self) -> list[Request]:
        """The requests that got a 2xx or 3xx answer: the only ones that exercise anything."""
        return [
            r
            for r in self.requests
            if r.status is not None and HTTPStatus.OK <= r.status < HTTPStatus.BAD_REQUEST
        ]


# --- the allowlist -----------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    """What one API's scope names: whole tags and single operations."""

    tags: tuple[str, ...]
    operations: tuple[str, ...]

    def includes(self, method: str, path: str, tags: tuple[str, ...]) -> bool:
        """Whether an operation is in the scope: one of its tags, or named by it."""
        named = {(m, normalize(p)) for m, p in (text.split(" ", 1) for text in self.operations)}
        return bool(set(tags) & set(self.tags)) or (method, normalize(path)) in named


@dataclass(frozen=True)
class Entry:
    """An in-scope operation no test sends, and why. An empty reason explains nothing."""

    api: str
    method: str
    path: str
    status: str
    reason: str

    @property
    def name(self) -> str:
        """The operation as the allowlist writes it."""
        return f"{self.method} {self.path}"

    @property
    def key(self) -> tuple[str, str, str]:
        """The identity of the operation the entry is about (see Operation.key)."""
        return (self.api, self.method, normalize(self.path))


@dataclass(frozen=True)
class SdkOnly:
    """A request that matches no in-scope operation of its API, and why the server sends it."""

    api: str
    method: str
    path: str
    reason: str
    pattern: re.Pattern[str] = field(compare=False, repr=False)

    @property
    def name(self) -> str:
        """The request as the allowlist writes it, with its API."""
        return f"{TITLES[self.api]} {self.method} {self.path}"


@dataclass
class Allowlist:
    """The scope, the out-of-scope reason, and the entries."""

    scope: dict[str, Scope]
    out_of_scope: str
    entries: list[Entry]
    sdk_only: list[SdkOnly]


def _split(text: object, where: str) -> tuple[str, str]:
    if not isinstance(text, str):
        msg = f"{where} needs a request such as `GET /v2/...`"
        raise CoverageError(msg)
    method, _, path = text.partition(" ")
    if method not in {m.upper() for m in HTTP_METHODS} or not path.startswith("/") or " " in path:
        msg = f"{where}: {text!r} is not an upper-case HTTP method and a path"
        raise CoverageError(msg)
    return method, path


def _reason(raw: dict[str, Any], where: str) -> str:
    reason = raw.get("reason", "")
    if not isinstance(reason, str):
        msg = f'{where}: "reason" must be a string'
        raise CoverageError(msg)
    return reason.strip()


def _api(raw: dict[str, Any], where: str) -> str:
    api = raw.get("api")
    if api not in APIS:
        msg = f'{where}: "api" must be one of {", ".join(APIS)}'
        raise CoverageError(msg)
    return str(api)


def _list(raw: object, of: type, where: str) -> list[Any]:
    if not isinstance(raw, list) or not all(isinstance(item, of) for item in raw):
        msg = f"{where} must be a list of {of.__name__}"
        raise CoverageError(msg)
    return raw


def _scope(raw: object, path: Path) -> dict[str, Scope]:
    if not isinstance(raw, dict) or set(raw) != set(APIS):
        msg = f'the allowlist {path} needs a "scope" object with {" and ".join(APIS)}'
        raise CoverageError(msg)
    scope: dict[str, Scope] = {}
    for api in APIS:
        where = f"the {api} scope of the allowlist {path}"
        item = raw[api]
        if not isinstance(item, dict) or set(item) != {"tags", "operations"}:
            msg = f'{where} needs "tags" and "operations"'
            raise CoverageError(msg)
        operations = _list(item["operations"], str, f"{where}: operations")
        for text in operations:
            _split(text, where)
        scope[api] = Scope(tuple(_list(item["tags"], str, f"{where}: tags")), tuple(operations))
    return scope


def load_allowlist(path: Path) -> Allowlist:
    """Read the allowlist.

    An entry without a reason is not an error here: the report counts it as no entry.

    Raises:
        CoverageError: The allowlist cannot be read, or is malformed: a missing or
            unknown field, a malformed request, an unknown status, or a repeated entry.

    """
    document = read_json(path, "the allowlist")
    expected = {"scope", "out_of_scope", "operations", "sdk_only"}
    if not isinstance(document, dict) or set(document) != expected:
        msg = f"the allowlist {path} must be an object of {', '.join(sorted(expected))}"
        raise CoverageError(msg)
    out_of_scope = document["out_of_scope"]
    if not isinstance(out_of_scope, str) or not out_of_scope.strip():
        msg = f'the allowlist {path} needs an "out_of_scope" reason'
        raise CoverageError(msg)
    entries: list[Entry] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(_list(document["operations"], dict, "operations")):
        where = f"operations entry {index} of the allowlist {path}"
        method, op_path = _split(raw.get("operation"), where)
        api, status = _api(raw, where), raw.get("status")
        if status not in ENTRY_STATUSES:
            msg = f'{where}: "status" must be one of {", ".join(ENTRY_STATUSES)}'
            raise CoverageError(msg)
        entry = Entry(api, method, op_path, str(status), _reason(raw, where))
        if entry.key in seen:
            msg = f"{where}: {entry.api} {entry.name} is listed twice"
            raise CoverageError(msg)
        seen.add(entry.key)
        entries.append(entry)
    sdk_only: list[SdkOnly] = []
    for index, raw in enumerate(_list(document["sdk_only"], dict, "sdk_only")):
        where = f"sdk_only entry {index} of the allowlist {path}"
        method, request_path = _split(raw.get("request"), where)
        item = SdkOnly(
            _api(raw, where),
            method,
            request_path,
            _reason(raw, where),
            template_pattern(request_path),
        )
        if any(other.name == item.name for other in sdk_only):
            msg = f"{where}: {item.name} is listed twice"
            raise CoverageError(msg)
        sdk_only.append(item)
    return Allowlist(_scope(document["scope"], path), out_of_scope.strip(), entries, sdk_only)


# --- the comparison ----------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One reason the report fails."""

    kind: str
    subject: str
    detail: str


@dataclass
class Result:
    """Where one in-scope operation stands.

    `e2e` says whether the end-to-end suite exercised it: yes, no, or not run.
    """

    operation: Operation
    status: str
    tests: list[str]
    reason: str
    e2e: str


@dataclass
class SdkOnlyResult:
    """The requests one sdk_only entry explains, or one unexplained request."""

    name: str
    reason: str
    requests: list[Request]


@dataclass
class Report:
    """Everything the report says."""

    inventories: dict[str, Inventory]
    results: list[Result]
    out_of_scope_reason: str
    sdk_only: list[SdkOnlyResult]
    findings: list[Finding]
    requests: int
    unanswered: int
    tests: int
    baselines: dict[str, Inventory]
    e2e: EndToEnd | None

    @property
    def exit_code(self) -> int:
        """1 when anything fails the report, else 0."""
        return FINDINGS if self.findings else PASS

    def count(self, api: str, status: str) -> int:
        """How many in-scope operations of `api` have `status`."""
        return sum(1 for r in self.results if r.operation.api == api and r.status == status)

    def staged(self, api: str, stage: str) -> list[Result]:
        """The results of the in-scope operations of `api` at `stage`."""
        return [r for r in self.results if r.operation.api == api and r.operation.stage == stage]


def _scope_findings(inventory: Inventory, scope: Scope) -> list[Finding]:
    findings: list[Finding] = []
    title = TITLES[inventory.api]
    for tag in sorted(set(scope.tags)):
        if not any(tag in op.tags for op in inventory.operations):
            findings.append(Finding("stale", f"{title} tag {tag}", "no operation has this tag"))
        if scope.tags.count(tag) > 1:
            findings.append(Finding("stale", f"{title} tag {tag}", "the scope lists it twice"))
    for text in dict.fromkeys(scope.operations):
        method, path = text.split(" ", 1)
        found = [
            op
            for op in inventory.operations
            if op.method == method and normalize(op.path) == normalize(path)
        ]
        if not found:
            findings.append(
                Finding("stale", f"{title} {text}", "a scope operation not in the inventory")
            )
        elif set(found[0].tags) & set(scope.tags):
            findings.append(
                Finding("stale", f"{title} {text}", "a scope operation a scope tag already has")
            )
        if scope.operations.count(text) > 1:
            findings.append(Finding("stale", f"{title} {text}", "the scope lists it twice"))
    findings += [
        Finding(
            "stale", f"{title} {op.name}", "in the inventory but not in the scope; rerun snapshot"
        )
        for op in inventory.operations
        if not scope.includes(op.method, op.path, op.tags)
    ]
    return findings


def _sent(
    inventories: dict[str, Inventory],
    origins: dict[str, tuple[str, ...]],
    requests: list[Request],
) -> tuple[dict[tuple[str, str, str], set[str]], list[Request]]:
    """Return the tests that got a response from each operation, and the unmatched requests.

    A request that got no response is neither.
    """
    tests: dict[tuple[str, str, str], set[str]] = {}
    unmatched: list[Request] = []
    for request in requests:
        if request.status is None:
            continue
        apis = origins.get(request.origin, ())
        matches = [
            op
            for op in (inventories[api].match(request.method, request.path) for api in apis)
            if op is not None
        ]
        if not matches:
            unmatched.append(request)
            continue
        best = max(matches, key=lambda op: (specificity(op.path), op.path))
        tests.setdefault(best.key, set()).add(request.test)
    return tests, unmatched


def _sdk_only(
    unmatched: list[Request],
    origins: dict[str, tuple[str, ...]],
    entries: list[SdkOnly],
) -> tuple[list[SdkOnlyResult], list[Finding]]:
    explained: dict[str, list[Request]] = {entry.name: [] for entry in entries}
    unexplained: dict[str, list[Request]] = {}
    for request in unmatched:
        apis = origins.get(request.origin, ())
        candidates = [
            entry
            for entry in entries
            if entry.api in apis
            and entry.method == request.method
            and entry.pattern.match(request.path)
        ]
        if candidates:
            entry = max(candidates, key=lambda e: (specificity(e.path), e.path))
            explained[entry.name].append(request)
        else:
            served = " and ".join(TITLES[api] for api in apis) or "no API in the origins file"
            name = f"{request.name} to {request.origin} ({served})"
            unexplained.setdefault(name, []).append(request)
    findings = [
        Finding(
            "unmatched",
            f"{name}, sent by {requests[0].test}",
            "matches no in-scope operation and no sdk_only entry: add it to the scope or to "
            "sdk_only",
        )
        for name, requests in sorted(unexplained.items())
    ]
    results = [SdkOnlyResult(name, "", requests) for name, requests in sorted(unexplained.items())]
    for entry in entries:
        if not explained[entry.name]:
            findings.append(Finding("stale", entry.name, "an sdk_only entry no request matches"))
        else:
            if not entry.reason:
                findings.append(
                    Finding("unmatched", entry.name, "the sdk_only entry has no reason")
                )
            results.append(SdkOnlyResult(entry.name, entry.reason, explained[entry.name]))
    return results, findings


def _results(
    inventories: dict[str, Inventory],
    allowlist: Allowlist,
    sent: dict[tuple[str, str, str], set[str]],
    exercised: set[tuple[str, str, str]] | None,
) -> tuple[list[Result], list[Finding]]:
    entries = {entry.key: entry for entry in allowlist.entries}
    results: list[Result] = []
    findings: list[Finding] = []
    for api, inventory in inventories.items():
        findings += _scope_findings(inventory, allowlist.scope[api])
        for operation in inventory.operations:
            label = f"{TITLES[api]} {operation.name}"
            tests = sorted(sent.get(operation.key, set()))
            entry = entries.pop(operation.key, None)
            e2e = NOT_RUN
            if exercised is not None:
                e2e = EXERCISED if operation.key in exercised else NOT_EXERCISED
            if tests:
                results.append(Result(operation, COVERED, tests, "", e2e))
                if entry is not None:
                    findings.append(Finding("stale", label, f"allowlisted as {entry.status}"))
            elif entry is not None and entry.reason:
                results.append(Result(operation, entry.status, [], entry.reason, e2e))
            else:
                status = entry.status if entry is not None else MISSING
                results.append(Result(operation, status, [], "", e2e))
                findings.append(
                    Finding(
                        "unexplained", label, "no test sends it and the allowlist gives no reason"
                    )
                )
    findings += [
        Finding("stale", f"{TITLES[entry.api]} {entry.name}", "allowlisted, but not in scope")
        for entry in entries.values()
    ]
    return results, findings


def _drift(current: Inventory, baseline: Inventory) -> list[Finding]:
    now = {op.key: op for op in current.operations}
    before = {op.key: op for op in baseline.operations}
    title = TITLES[current.api]
    findings: list[Finding] = []
    for key in sorted(set(now) | set(before)):
        new, old = now.get(key), before.get(key)
        if old is None and new is not None:
            findings.append(Finding("drift", f"{title} {new.name}", "added since the snapshot"))
        elif new is None and old is not None:
            findings.append(Finding("drift", f"{title} {old.name}", "removed since the snapshot"))
        elif new is not None and old is not None:
            changes = [
                f"{what} {was!r} -> {is_!r}"
                for what, was, is_ in (
                    ("path", old.path, new.path),
                    ("operationId", old.operation_id, new.operation_id),
                    ("tags", list(old.tags), list(new.tags)),
                    ("deprecated", old.deprecated, new.deprecated),
                    ("parameters", _params(old), _params(new)),
                    ("requestBody", old.request_body, new.request_body),
                )
                if was != is_
            ]
            if changes:
                findings.append(Finding("drift", f"{title} {new.name}", "; ".join(changes)))
    return findings


def _params(operation: Operation) -> list[str]:
    return [
        f"{where} {name}{' (required)' if required else ''}"
        for where, name, required in operation.parameters
    ]


def build_report(  # noqa: PLR0913 - five inputs, and the optional e2e one keyword-only
    inventories: dict[str, Inventory],
    requests: list[Request],
    origins: dict[str, tuple[str, ...]],
    allowlist: Allowlist,
    baselines: dict[str, Inventory],
    *,
    e2e: EndToEnd | None = None,
) -> Report:
    """Compare the inventories with the record and the allowlist.

    Args:
        inventories: The inventory of each API.
        requests: The recorded requests.
        origins: The APIs each origin serves.
        allowlist: The scope and the reasons.
        baselines: Inventories to check for drift against, by API.
        e2e: What the end-to-end suite sent, for the end-to-end column; None when it
            did not run.

    Returns:
        The report.

    """
    sent, unmatched = _sent(inventories, origins, requests)
    exercised = None
    if e2e is not None:
        exercised = set(_sent(inventories, e2e.origins, e2e.answered)[0])
    results, findings = _results(inventories, allowlist, sent, exercised)
    sdk_only, sdk_findings = _sdk_only(unmatched, origins, allowlist.sdk_only)
    for api, baseline in baselines.items():
        findings += _drift(inventories[api], baseline)
    return Report(
        inventories=inventories,
        results=results,
        out_of_scope_reason=allowlist.out_of_scope,
        sdk_only=sdk_only,
        findings=findings + sdk_findings,
        requests=len(requests),
        unanswered=sum(1 for request in requests if request.status is None),
        tests=len({request.test for request in requests}),
        baselines=baselines,
        e2e=e2e,
    )


# --- rendering ---------------------------------------------------------------------


def _cell(text: object) -> str:
    """Make outside text safe in a Markdown table cell."""
    return " ".join(str(text).split()).replace("|", "\\|").replace("`", "'")


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _code(text: object) -> str:
    return f"`{_cell(text)}`"


def _row(*cells: object) -> str:
    return "| " + " | ".join(str(cell) for cell in cells) + " |"


FINDING_TITLES = {
    "unexplained": "In-scope operations no test sends, with no reason",
    "unmatched": "Requests that match no in-scope operation, with no reason",
    "stale": "Stale allowlist entries",
    "drift": "In-scope operations that changed since the snapshot",
}


def render(report: Report) -> str:
    """Render the Markdown report, ending with a newline."""
    out = ["## API coverage", ""]
    if report.findings:
        out.append(f":x: **{len(report.findings)} finding(s).**")
    else:
        out.append(":white_check_mark: **Every in-scope operation is covered or explained.**")
    out.append("")
    out += [f"- {TITLES[api]}: {inv.source}." for api, inv in report.inventories.items()]
    out += [f"- {TITLES[api]} baseline: {inv.source}." for api, inv in report.baselines.items()]
    record = f"{_plural(report.requests, 'request')} from {_plural(report.tests, 'test')}"
    unanswered = _plural(report.unanswered, "request")
    out.append(f"- Record: {record}; {unanswered} got no response and count for nothing.")
    out.append(f"- End-to-end record: {_describe_e2e(report.e2e)}.")
    out.append("")
    statuses = (COVERED, UNTESTED, MISSING)
    out += [
        _row("API", "In scope", *(s.capitalize() for s in statuses)),
        _row(*["---"] * 5),
    ]
    for api in report.inventories:
        counts = [report.count(api, status) for status in statuses]
        out.append(_row(TITLES[api], sum(counts), *counts))
    out += ["", "### By stage", ""]
    out += [
        _row("API", "Stage", "In scope", *(s.capitalize() for s in statuses), "End to end"),
        _row(*["---"] * 7),
    ]
    for api in report.inventories:
        for stage in STAGES:
            results = report.staged(api, stage)
            if results:
                counts = [sum(1 for r in results if r.status == s) for s in statuses]
                e2e = NOT_RUN if report.e2e is None else sum(r.e2e == EXERCISED for r in results)
                out.append(_row(TITLES[api], stage, len(results), *counts, e2e))
    out += ["", f"SDK-only requests: {len(report.sdk_only)}.", ""]
    out += [f"Out of scope: {_cell(report.out_of_scope_reason)}", ""]
    for kind, title in FINDING_TITLES.items():
        listed = [f for f in report.findings if f.kind == kind]
        if listed:
            out += [f"### {title}", ""]
            out += [f"- {_code(f.subject)}: {_cell(f.detail)}" for f in listed]
            out.append("")
    out += ["### In-scope operations", ""]
    out += [
        _row(
            "API",
            "Operation",
            "operationId",
            "Stage",
            "Status",
            "Tests or reason",
            "Exercised end to end",
        ),
        _row(*["---"] * 7),
    ]
    for result in report.results:
        detail = (
            _plural(len(result.tests), "test") if result.tests else _cell(result.reason or "none")
        )
        out.append(
            _row(
                TITLES[result.operation.api],
                _code(result.operation.name),
                _code(result.operation.operation_id),
                result.operation.stage,
                result.status,
                detail,
                result.e2e,
            )
        )
    if report.sdk_only:
        out += ["", "### SDK-only requests", ""]
        out += [_row("Request", "Requests", "Reason"), _row(*["---"] * 3)]
        out += [
            _row(_code(r.name), len(r.requests), _cell(r.reason or "none")) for r in report.sdk_only
        ]
    out.append("")
    return "\n".join(out)


def _describe_e2e(e2e: EndToEnd | None) -> str:
    if e2e is None:
        return f"{NOT_RUN}, so the end-to-end column says {NOT_RUN}"
    tests = len({request.test for request in e2e.requests})
    answered = _plural(len(e2e.answered), "request")
    return (
        f"{_plural(len(e2e.requests), 'request')} from {_plural(tests, 'test')}; "
        f"{answered} got a 2xx or 3xx answer and count"
    )


def _emit(text: str, summary: str | None) -> None:
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    else:
        print(text)


def _annotation(text: object) -> str:
    """Make outside text safe in a GitHub annotation's message."""
    return str(text).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


# --- the command line --------------------------------------------------------------


def _named(values: list[str] | None, option: str, allowed: tuple[str, ...]) -> dict[str, Path]:
    named: dict[str, Path] = {}
    for value in values or []:
        name, sep, rest = value.partition("=")
        if not sep or name not in allowed or not rest or name in named:
            msg = f"{option} takes NAME=PATH once per NAME, NAME one of {', '.join(allowed)}"
            raise CoverageError(msg)
        named[name] = Path(rest)
    return named


def run(args: argparse.Namespace) -> Report:
    """Read every input and build the report.

    Raises:
        CoverageError: An input failed its checks.

    """
    specs = _named(args.spec, "--spec", APIS)
    if set(specs) != set(APIS):
        msg = f"--spec is needed for each of {', '.join(APIS)}"
        raise CoverageError(msg)
    inventories = {api: load_inventory(api, specs[api]) for api in APIS}
    baselines = {
        api: load_inventory(api, path)
        for api, path in _named(args.baseline, "--baseline", (CONTROL_PLANE,)).items()
    }
    allowlist = load_allowlist(Path(args.allowlist))
    requests = load_record(Path(args.record))
    origins = load_origins(Path(args.origins))
    return build_report(inventories, requests, origins, allowlist, baselines, e2e=_end_to_end(args))


def _end_to_end(args: argparse.Namespace) -> EndToEnd | None:
    if args.e2e_record is None and args.e2e_origins is None:
        return None
    if args.e2e_record is None or args.e2e_origins is None:
        msg = "--e2e-record and --e2e-origins are given together or not at all"
        raise CoverageError(msg)
    return EndToEnd(
        load_e2e_record(Path(args.e2e_record)),
        load_origins(Path(args.e2e_origins), "the end-to-end origins file"),
    )


def report_command(args: argparse.Namespace) -> int:
    """Run the report and write it; return 0, 1 or 2."""
    try:
        report = run(args)
    except CoverageError as exc:
        reason = str(exc)
    # Any other error is also a report that did not run, never a finding.
    except Exception as exc:  # noqa: BLE001 - mapped to exit 2, with its traceback on stderr
        traceback.print_exc()
        reason = f"{type(exc).__name__}: {exc}"
    else:
        _emit(render(report), args.summary)
        for finding in report.findings:
            print(
                f"::error title=API coverage::{_annotation(finding.kind)}: "
                f"{_annotation(finding.subject)}: {_annotation(finding.detail)}"
            )
        return report.exit_code
    print(f"::error title=API coverage::The report did not run: {_annotation(reason)}")
    _emit(
        f"## API coverage\n\n:warning: **The report did not run**, so there is no result.\n\n"
        f"{_cell(reason)}\n",
        args.summary,
    )
    return DID_NOT_RUN


def snapshot_command(args: argparse.Namespace) -> int:
    """Write the in-scope inventory of a downloaded spec and its source file; return 0 or 2."""
    try:
        scope = load_allowlist(Path(args.allowlist)).scope[args.api]
        operations = _snapshot_operations(args.api, Path(args.spec))
    except CoverageError as exc:
        print(f"::error title=API spec snapshot::{_annotation(exc)}")
        return DID_NOT_RUN
    kept = [op for op in operations if scope.includes(op["method"], op["path"], tuple(op["tags"]))]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    inventory = out_dir / f"{args.api}.json"
    fetched = args.fetched or dt.datetime.now(dt.UTC).date().isoformat()
    _write(inventory, {"operations": kept})
    _write(source_file(inventory), {"source": args.source, "fetched": fetched})
    kept_text = _plural(len(kept), "in-scope operation")
    print(
        f"Wrote {kept_text} of the {_plural(len(operations), 'operation')} of the "
        f"{TITLES[args.api]} spec to {inventory}."
    )
    return PASS


def compare_command(args: argparse.Namespace) -> int:
    """Compare a downloaded spec's in-scope operations with an inventory; return 0, 1 or 2."""
    spec, inventory = Path(args.spec), Path(args.inventory)
    try:
        scope = load_allowlist(Path(args.allowlist)).scope[args.api]
        operations = _snapshot_operations(args.api, spec)
        committed = load_inventory(args.api, inventory)
        kept = [
            op for op in operations if scope.includes(op["method"], op["path"], tuple(op["tags"]))
        ]
        published = Inventory(
            api=args.api,
            source=str(spec),
            operations=[
                _operation(args.api, op, f"{op['method']} {op['path']} of {spec}") for op in kept
            ],
        )
        committed_text = inventory.read_text(encoding="utf-8")
    except (CoverageError, OSError) as exc:
        print(f"::error title=API spec comparison::{_annotation(exc)}")
        return DID_NOT_RUN
    differences = _drift(published, committed)
    in_scope = _plural(len(kept), "in-scope operation")
    if not differences:
        print(f"{spec} matches {inventory}: {in_scope}.")
        return PASS
    title = TITLES[args.api]
    print(f"{spec} differs from {inventory}, the committed {title} inventory ({in_scope}):")
    print("\n".join(f"- {d.subject}: {d.detail}" for d in differences))
    published_text = json.dumps({"operations": kept}, indent=2, sort_keys=True) + "\n"
    print(
        "".join(
            difflib.unified_diff(
                committed_text.splitlines(keepends=True),
                published_text.splitlines(keepends=True),
                fromfile=str(inventory),
                tofile=f"{spec}, in scope",
            )
        ),
        end="",
    )
    print(
        f"::error title=API spec comparison::{_annotation(spec)} differs from the committed "
        f"{title} inventory {_annotation(inventory)}: {len(differences)} difference(s)"
    )
    return FINDINGS


def _snapshot_operations(api: str, spec: Path) -> list[dict[str, Any]]:
    label = f"the {TITLES[api]} spec"
    operations = operations_of(read_json(spec, label), f"{label} {spec}")
    if len(operations) < MIN_OPERATIONS[api]:
        msg = (
            f"{label} {spec} lists {len(operations)} operations, fewer than the minimum of "
            f"{MIN_OPERATIONS[api]}; it is truncated or not the spec"
        )
        raise CoverageError(msg)
    return operations


def _write(path: Path, document: object) -> None:
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parser() -> argparse.ArgumentParser:
    """Build the command line: `report` and `snapshot`."""
    root = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    commands = root.add_subparsers(dest="command", required=True)

    report = commands.add_parser("report", help="compare the inventories with a record")
    report.add_argument("--spec", action="append", metavar="API=PATH", help="an API's inventory")
    report.add_argument("--allowlist", required=True, help="the scope and reasons (JSON)")
    report.add_argument("--record", required=True, help="the offline tests' request record")
    report.add_argument("--origins", required=True, help="the APIs each test server serves")
    report.add_argument(
        "--baseline",
        action="append",
        metavar="control-plane=PATH",
        help="an inventory the in-scope operations must not have drifted from",
    )
    report.add_argument("--e2e-record", help="the end-to-end suite's request record")
    report.add_argument("--e2e-origins", help="the APIs each origin of the e2e record serves")
    report.add_argument("--summary", help="append the Markdown report here, not to stdout")
    report.set_defaults(handler=report_command)

    snapshot = commands.add_parser("snapshot", help="write a spec's in-scope inventory")
    snapshot.add_argument("api", choices=APIS)
    snapshot.add_argument("spec", help="the downloaded OpenAPI document")
    snapshot.add_argument("--allowlist", required=True, help="the allowlist with the scope")
    snapshot.add_argument("--source", required=True, help="where the document came from")
    snapshot.add_argument("--fetched", help="when it was fetched (default: today, UTC)")
    snapshot.add_argument("--out-dir", default=".github/api-specs", help="%(default)s")
    snapshot.set_defaults(handler=snapshot_command)

    compare = commands.add_parser("compare", help="compare a spec with a committed inventory")
    compare.add_argument("api", choices=APIS)
    compare.add_argument("spec", help="the downloaded OpenAPI document")
    compare.add_argument("--inventory", required=True, help="the committed inventory")
    compare.add_argument("--allowlist", required=True, help="the allowlist with the scope")
    compare.set_defaults(handler=compare_command)
    return root


def main(argv: list[str] | None = None) -> int:
    """Run a subcommand and return its exit status; bad arguments exit 2 via argparse."""
    args = parser().parse_args(argv)
    try:
        status: int = args.handler(args)
    except Exception:  # noqa: BLE001 - writing the outputs failed: no result, exit 2
        traceback.print_exc()
        return DID_NOT_RUN
    return status


if __name__ == "__main__":
    sys.exit(main())
