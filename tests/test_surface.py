"""The surface clients and hosts build on, pinned in tests/snapshots/surface.json.

The snapshot holds every tool as an MCP client lists it (with both elements set, so every
tool is registered): its description, input and output schemas and annotations. It also
holds `permit_mcp.__all__` and the signatures of the public functions and classes. Any
difference fails the test with a diff. After an intended change, rewrite the snapshot with

    UPDATE_SNAPSHOT=1 uv run pytest tests/test_surface.py

and commit it. On a pull request, CI labels each change against the base branch's snapshot
BREAKING or non-breaking (.github/scripts/surface_diff.py).
"""

from __future__ import annotations

import difflib
import inspect
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import permit_mcp
from permit_mcp import PermitTools, Settings, access_token_subject, bound_user, create_server
from tests.support import connected

if TYPE_CHECKING:
    from collections.abc import Callable

SNAPSHOT = Path(__file__).parent / "snapshots" / "surface.json"
UPDATE_COMMAND = "UPDATE_SNAPSHOT=1 uv run pytest tests/test_surface.py"
SIGNED: tuple[Callable[..., Any], ...] = (
    create_server,
    bound_user,
    access_token_subject,
    PermitTools,
    Settings,
)
KINDS = {
    inspect.Parameter.POSITIONAL_ONLY: "positional-only",
    inspect.Parameter.POSITIONAL_OR_KEYWORD: "positional-or-keyword",
    inspect.Parameter.VAR_POSITIONAL: "var-positional",
    inspect.Parameter.KEYWORD_ONLY: "keyword-only",
    inspect.Parameter.VAR_KEYWORD: "var-keyword",
}
# "collections.abc.Collection[str]" -> "Collection[str]": the module an annotation's type
# lives in can move between dependency versions without the signature changing.
_MODULE_PREFIX = re.compile(r"\b[A-Za-z_]\w*\.(?=[A-Za-z_])")


def annotation_text(annotation: object) -> str:
    """Return an annotation as text, without the modules its names come from."""
    return _MODULE_PREFIX.sub("", inspect.formatannotation(annotation))


def signature_of(obj: Callable[..., Any], *, method: bool = False) -> dict[str, Any]:
    """Return the parameters, return annotation and async-ness of a function or class.

    For a `method`, the first parameter (self) is left out. A parameter without an
    annotation has no "annotation" key, and one without a default no "default" key.
    """
    signature = inspect.signature(obj, eval_str=True)
    parameters: list[dict[str, str]] = []
    for parameter in list(signature.parameters.values())[1 if method else 0 :]:
        entry = {"name": parameter.name, "kind": KINDS[parameter.kind]}
        if parameter.annotation is not parameter.empty:
            entry["annotation"] = annotation_text(parameter.annotation)
        if parameter.default is not parameter.empty:
            entry["default"] = repr(parameter.default)
        parameters.append(entry)
    described: dict[str, Any] = {
        "async": inspect.iscoroutinefunction(obj),
        "parameters": parameters,
    }
    if not inspect.isclass(obj) and signature.return_annotation is not signature.empty:
        described["return"] = annotation_text(signature.return_annotation)
    return described


def public_signatures() -> dict[str, dict[str, Any]]:
    """Return the signature of each public function and class, and of the classes' methods."""
    signatures: dict[str, dict[str, Any]] = {}
    for obj in SIGNED:
        signatures[obj.__name__] = signature_of(obj)
        if not inspect.isclass(obj):
            continue
        for name, member in vars(obj).items():
            # mutmut adds a class's mutants to it under non-ASCII names (xǁClassǁname__mutmut_1).
            if name.startswith("_") or not name.isascii():
                continue
            if isinstance(member, (classmethod, staticmethod)):
                signatures[f"{obj.__name__}.{name}"] = signature_of(getattr(obj, name))
            elif inspect.isfunction(member):
                signatures[f"{obj.__name__}.{name}"] = signature_of(member, method=True)
    return signatures


async def current_surface(settings: Settings) -> dict[str, Any]:
    """Return the surface as an MCP client and an importer of permit_mcp see it."""
    async with connected(settings) as client:
        listed = (await client.list_tools()).tools
    tools = {
        tool.name: {
            "description": tool.description,
            "input_schema": tool.input_schema,
            "output_schema": tool.output_schema,
            "annotations": (
                None
                if tool.annotations is None
                else tool.annotations.model_dump(mode="json", by_alias=True, exclude_none=True)
            ),
        }
        for tool in listed
    }
    return {
        "tools": tools,
        "public": {"__all__": sorted(permit_mcp.__all__), "signatures": public_signatures()},
    }


def render(surface: dict[str, Any]) -> str:
    return json.dumps(surface, indent=2, sort_keys=True) + "\n"


async def test_the_surface_matches_the_snapshot(settings: Settings) -> None:
    current = render(await current_surface(settings))
    if os.environ.get("UPDATE_SNAPSHOT") == "1":
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(current, encoding="utf-8")
    committed = SNAPSHOT.read_text(encoding="utf-8") if SNAPSHOT.exists() else ""
    if current != committed:
        diff = "".join(
            difflib.unified_diff(
                committed.splitlines(keepends=True),
                current.splitlines(keepends=True),
                fromfile="tests/snapshots/surface.json (committed)",
                tofile="the surface now",
            )
        )
        message = (
            f"The tool or public API surface differs from the snapshot:\n{diff}\n"
            f"If the change is intended, rewrite the snapshot with `{UPDATE_COMMAND}`, "
            "commit it, and say in CHANGELOG.md what changed for users."
        )
        raise AssertionError(message)


def test_the_snapshot_holds_every_tool_and_public_name() -> None:
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    assert set(snapshot["tools"]) == set(permit_mcp.TOOL_NAMES)
    assert snapshot["public"]["__all__"] == sorted(permit_mcp.__all__)
    for tool in snapshot["tools"].values():
        assert set(tool) == {"description", "input_schema", "output_schema", "annotations"}
    assert set(snapshot["public"]["signatures"]) == {
        "create_server",
        "bound_user",
        "access_token_subject",
        "PermitTools",
        "PermitTools.register",
        "PermitTools.aclose",
        "Settings",
        "Settings.from_env",
    }


def test_signatures_record_kinds_defaults_and_async() -> None:
    signatures = public_signatures()

    assert signatures["create_server"]["parameters"][1] == {
        "name": "identity",
        "kind": "keyword-only",
        "annotation": "IdentityResolver | None",
        "default": "None",
    }
    assert signatures["create_server"]["return"] == "MCPServer[Any]"
    assert signatures["PermitTools.aclose"]["async"] is True
    settings = {field["name"]: field for field in signatures["Settings"]["parameters"]}
    assert "default" not in settings["api_key"]
    assert settings["tenant"]["default"] == "'default'"
    assert settings["user"]["annotation"] == "str | None"


def planted(  # type: ignore[no-untyped-def]  # the unannotated parameter is the point
    items: dict[str, Path],
    other: inspect.Parameter | None,
    bare,  # noqa: ANN001
) -> None:
    """A signature with annotations from other modules, and none."""


def test_annotation_text_drops_module_paths() -> None:
    parameters = signature_of(planted)["parameters"]

    assert [parameter.get("annotation") for parameter in parameters] == [
        "dict[str, Path]",
        "Parameter | None",
        None,
    ]
