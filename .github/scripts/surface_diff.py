#!/usr/bin/env python3
"""Compare two surface snapshots and label each change BREAKING or non-breaking.

Usage: surface_diff.py BASE HEAD

BASE and HEAD are copies of tests/snapshots/surface.json, such as the base branch's
and a pull request's. Prints each change, then a count. Stdlib only.

A change is breaking when a client or a host written for BASE can fail under HEAD:

- a tool removed;
- an argument removed, newly required, no longer accepting a type it accepted, or
  given new validation: an enum or a constraint (minLength, maximum, pattern,
  format, ...) added, an enum value removed, or a bound tightened;
- a result field removed or made optional, a result type widened, an output schema
  removed, or a constraint on a result loosened (the mirror image of arguments:
  clients read results);
- a public name removed from `permit_mcp.__all__`, or a public signature removed or
  changed: a parameter removed, newly required, retyped, or no longer passable as
  it was; positional parameters reordered; a return type changed; sync made async
  or the reverse.

The same rules apply inside an array's `items` schema and an object's
`additionalProperties` schema. A schema without a type allows any type, so declaring
one narrows it: breaking for an argument, not for a result.

Anything else, such as an added tool, an added optional argument, a loosened
argument constraint, a changed description, title, default or tool annotation, is
non-breaking. A constraint whose old and new values cannot be ordered, such as a
changed pattern, is breaking.

Exits 0 when no change is breaking (there may be non-breaking ones), 1 when at
least one is, and 2 when it could not compare: wrong arguments, or a file that
cannot be read, is not JSON, or is not a surface snapshot.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

NO_BREAKING = 0
BREAKING = 1
DID_NOT_RUN = 2

Direction = Literal["input", "output"]

# Bounds that, raised, accept fewer values; and those that, lowered, accept fewer.
_LOWER_BOUNDS = ("minLength", "minimum", "exclusiveMinimum", "minItems", "minProperties")
_UPPER_BOUNDS = ("maxLength", "maximum", "exclusiveMaximum", "maxItems", "maxProperties")
# Constraints whose values cannot be ordered: adding one narrows what is accepted.
_OTHER_CONSTRAINTS = ("pattern", "format", "multipleOf", "const", "uniqueItems")
_COMBINATORS = ("anyOf", "oneOf", "allOf")
_KINDS = ("positional-only", "positional-or-keyword", "keyword-only")
_ALL_KINDS = (*_KINDS, "var-positional", "var-keyword")


class NotASnapshotError(ValueError):
    """The file is not a surface snapshot."""


@dataclass(frozen=True)
class Change:
    """One difference between the base and the head snapshot."""

    breaking: bool
    subject: str
    detail: str

    def __str__(self) -> str:
        label = "BREAKING" if self.breaking else "non-breaking"
        return f"{label:<12}  {self.subject}: {self.detail}"


class _Differ:
    def __init__(self) -> None:
        self.changes: list[Change] = []
        self.subject = ""

    def add(self, *, breaking: bool, detail: str) -> None:
        self.changes.append(Change(breaking, self.subject, detail))


# --- tools ------------------------------------------------------------------


def _resolve(schema: Any, defs: dict[str, Any], seen: frozenset[str] = frozenset()) -> Any:  # noqa: ANN401 - JSON
    """Return `schema` with every local `$ref` replaced by the definition it names."""
    if isinstance(schema, list):
        return [_resolve(item, defs, seen) for item in schema]
    if not isinstance(schema, dict):
        return schema
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/") and ref not in seen:
        target = defs.get(ref.removeprefix("#/$defs/"), {})
        rest = {key: value for key, value in schema.items() if key != "$ref"}
        return _resolve({**target, **rest}, defs, seen | {ref})
    return {key: _resolve(value, defs, seen) for key, value in schema.items() if key != "$defs"}


def _branches(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the schema itself and every branch of its anyOf, oneOf and allOf, flattened."""
    found = [schema]
    for key in _COMBINATORS:
        for branch in schema.get(key) or []:
            if isinstance(branch, dict):
                found.extend(_branches(branch))
    return found


@dataclass
class _Facets:
    """What a property schema accepts, merged over its anyOf, oneOf and allOf branches."""

    types: set[str]
    enum: set[str] | None
    constraints: dict[str, Any]
    properties: dict[str, Any]
    required: set[str]
    notes: dict[str, Any]
    # The schemas of an array's items and of an object's additional properties.
    items: dict[str, Any] | None = None
    values: dict[str, Any] | None = None


def _facets(schema: object) -> _Facets:
    facets = _Facets(set(), None, {}, {}, set(), {})
    for branch in _branches(schema if isinstance(schema, dict) else {}):
        _add_branch(facets, branch)
    return facets


def _add_branch(facets: _Facets, branch: dict[str, Any]) -> None:
    """Merge what one branch of a schema allows into `facets`."""
    declared = branch.get("type")
    if isinstance(declared, str):
        facets.types.add(declared)
    elif isinstance(declared, list):
        facets.types.update(str(item) for item in declared)
    _add_nested(facets, branch)
    if "enum" in branch:
        facets.enum = (facets.enum or set()) | {json.dumps(v) for v in branch["enum"]}
    for key in (*_LOWER_BOUNDS, *_UPPER_BOUNDS, *_OTHER_CONSTRAINTS):
        if key in branch:
            facets.constraints[key] = branch[key]
    facets.properties.update(branch.get("properties") or {})
    facets.required.update(branch.get("required") or [])
    for key in ("description", "title", "default"):
        if key in branch:
            facets.notes[key] = branch[key]


def _add_nested(facets: _Facets, branch: dict[str, Any]) -> None:
    """Merge a branch's items schema and additionalProperties into `facets`."""
    extra = branch.get("additionalProperties")
    if extra is False:
        facets.constraints["additionalProperties"] = False
    elif isinstance(extra, dict):
        facets.values = {**(facets.values or {}), **extra}
    if isinstance(branch.get("items"), dict):
        facets.items = {**(facets.items or {}), **branch["items"]}


def _narrowed(direction: Direction) -> bool:
    """Whether accepting fewer values breaks: for arguments yes, for results no."""
    return direction == "input"


def _compare_constraint(key: str, old: Any, new: Any) -> str | None:  # noqa: ANN401 - JSON
    """Return "tightened", "loosened" or "changed" for a constraint's old and new value."""
    if old == new:
        return None
    if old is None:
        return "tightened"
    if new is None:
        return "loosened"
    if key in _LOWER_BOUNDS and isinstance(old, (int, float)) and isinstance(new, (int, float)):
        return "tightened" if new > old else "loosened"
    if key in _UPPER_BOUNDS and isinstance(old, (int, float)) and isinstance(new, (int, float)):
        return "tightened" if new < old else "loosened"
    return "changed"


def _schema_changes(
    differ: _Differ, what: str, base: dict[str, Any], head: dict[str, Any], direction: Direction
) -> None:
    old, new = _facets(base), _facets(head)
    narrowing = _narrowed(direction)
    _type_changes(differ, what, old.types, new.types, narrowing=narrowing)
    _enum_changes(differ, what, old.enum, new.enum, narrowing=narrowing)
    for key in sorted(old.constraints.keys() | new.constraints.keys()):
        before, after = old.constraints.get(key), new.constraints.get(key)
        verdict = _compare_constraint(key, before, after)
        if verdict is None:
            continue
        breaking = verdict == "changed" or (verdict == "tightened") == narrowing
        differ.add(
            breaking=breaking,
            detail=f"{what} {key} {verdict} ({_show(before)} -> {_show(after)})",
        )
    for key in sorted(old.notes.keys() | new.notes.keys()):
        if old.notes.get(key) != new.notes.get(key):
            differ.add(breaking=False, detail=f"{what} {key} changed")
    _properties_changes(differ, what, old, new, direction)
    # A schema left out allows anything, so the comparison runs whenever either side has one.
    if old.items is not None or new.items is not None:
        _schema_changes(differ, f"{what} items", old.items or {}, new.items or {}, direction)
    if old.values is not None or new.values is not None:
        _schema_changes(
            differ, f"{what} property values", old.values or {}, new.values or {}, direction
        )


def _type_changes(
    differ: _Differ, what: str, old: set[str], new: set[str], *, narrowing: bool
) -> None:
    # No type is any type: declaring one narrows what is allowed, dropping all widens it.
    if not old and new:
        allowed = " | ".join(sorted(new))
        differ.add(breaking=narrowing, detail=f"{what} now limited to type {allowed}")
        return
    if old and not new:
        differ.add(breaking=not narrowing, detail=f"{what} now allows any type")
        return
    lost, gained = sorted(old - new), sorted(new - old)
    if lost and gained:
        before, after = " | ".join(sorted(old)), " | ".join(sorted(new))
        differ.add(breaking=True, detail=f"{what} type changed from {before} to {after}")
        return
    if lost:
        differ.add(breaking=narrowing, detail=f"{what} no longer allows {', '.join(lost)}")
    if gained:
        differ.add(breaking=not narrowing, detail=f"{what} now allows {', '.join(gained)}")


def _enum_changes(
    differ: _Differ, what: str, old: set[str] | None, new: set[str] | None, *, narrowing: bool
) -> None:
    if old == new:
        return
    if old is None:
        differ.add(breaking=narrowing, detail=f"{what} now limited to {_values(new)}")
    elif new is None:
        differ.add(breaking=not narrowing, detail=f"{what} no longer limited to a set")
    else:
        if old - new:
            differ.add(breaking=narrowing, detail=f"{what} values removed: {_values(old - new)}")
        if new - old:
            differ.add(breaking=not narrowing, detail=f"{what} values added: {_values(new - old)}")


def _properties_changes(
    differ: _Differ, what: str, old: _Facets, new: _Facets, direction: Direction
) -> None:
    noun = "argument" if direction == "input" else "result field"
    for name in sorted(old.properties.keys() | new.properties.keys()):
        path = f'{noun} "{name}"' if what in {"arguments", "result"} else f"{what}.{name}"
        if name not in new.properties:
            differ.add(breaking=True, detail=f"{path} removed")
            continue
        required = name in new.required
        if name not in old.properties:
            breaking = direction == "input" and required
            kind = "required" if required else "optional"
            differ.add(breaking=breaking, detail=f"{path} added ({kind})")
            continue
        was_required = name in old.required
        if was_required != required:
            # An argument that must now be sent breaks callers; a result field that may now
            # be absent breaks readers.
            breaking = required if direction == "input" else was_required
            state = "now required" if required else "now optional"
            differ.add(breaking=breaking, detail=f"{path} {state}")
        _schema_changes(differ, path, old.properties[name], new.properties[name], direction)


def _tool_changes(differ: _Differ, base: dict[str, Any], head: dict[str, Any]) -> None:
    if base.get("description") != head.get("description"):
        differ.add(breaking=False, detail="description changed")
    old_hints, new_hints = base.get("annotations") or {}, head.get("annotations") or {}
    for key in sorted(old_hints.keys() | new_hints.keys()):
        if old_hints.get(key) != new_hints.get(key):
            before, after = _show(old_hints.get(key)), _show(new_hints.get(key))
            differ.add(breaking=False, detail=f"annotation {key} changed ({before} -> {after})")
    old_input = _resolve(base["input_schema"], base["input_schema"].get("$defs", {}))
    new_input = _resolve(head["input_schema"], head["input_schema"].get("$defs", {}))
    _schema_changes(differ, "arguments", old_input, new_input, "input")
    old_output, new_output = base.get("output_schema"), head.get("output_schema")
    if old_output is None and new_output is not None:
        differ.add(breaking=False, detail="output schema added")
    elif old_output is not None and new_output is None:
        differ.add(breaking=True, detail="output schema removed")
    elif old_output is not None and new_output is not None:
        old_output = _resolve(old_output, old_output.get("$defs", {}))
        new_output = _resolve(new_output, new_output.get("$defs", {}))
        _schema_changes(differ, "result", old_output, new_output, "output")


# --- the public API ---------------------------------------------------------


def _parameter_changes(
    differ: _Differ, base: list[dict[str, Any]], head: list[dict[str, Any]]
) -> None:
    old = {parameter["name"]: parameter for parameter in base}
    new = {parameter["name"]: parameter for parameter in head}
    clean = True
    for name in [*old, *(name for name in new if name not in old)]:
        before, after = old.get(name), new.get(name)
        if after is None:
            differ.add(breaking=True, detail=f'parameter "{name}" removed')
            clean = False
            continue
        if before is None:
            required = "default" not in after and not after["kind"].startswith("var-")
            kind = "required" if required else "optional"
            differ.add(breaking=required, detail=f'parameter "{name}" added ({kind})')
            continue
        if before["kind"] != after["kind"]:
            widened = after["kind"] == "positional-or-keyword" and before["kind"] in _KINDS
            differ.add(
                breaking=not widened,
                detail=f'parameter "{name}" changed from {before["kind"]} to {after["kind"]}',
            )
            clean = clean and widened
        if before.get("annotation") != after.get("annotation"):
            differ.add(
                breaking=True,
                detail=f'parameter "{name}" type changed from '
                f"{before.get('annotation', 'unannotated')} to "
                f"{after.get('annotation', 'unannotated')}",
            )
        if "default" in before and "default" not in after:
            differ.add(breaking=True, detail=f'parameter "{name}" is now required')
        elif "default" not in before and "default" in after:
            differ.add(breaking=False, detail=f'parameter "{name}" is now optional')
        elif before.get("default") != after.get("default"):
            differ.add(
                breaking=False,
                detail=f'parameter "{name}" default changed from {before["default"]} '
                f"to {after['default']}",
            )
    positional = ("positional-only", "positional-or-keyword")
    old_order = [parameter["name"] for parameter in base if parameter["kind"] in positional]
    new_order = [parameter["name"] for parameter in head if parameter["kind"] in positional]
    if clean and new_order[: len(old_order)] != old_order:
        differ.add(
            breaking=True,
            detail=f"positional parameters changed from ({', '.join(old_order)}) "
            f"to ({', '.join(new_order)})",
        )


def _signature_changes(differ: _Differ, base: dict[str, Any], head: dict[str, Any]) -> None:
    if base.get("async") != head.get("async"):
        before, after = ("async", "sync") if base.get("async") else ("sync", "async")
        differ.add(breaking=True, detail=f"changed from {before} to {after}")
    if base.get("return") != head.get("return"):
        differ.add(
            breaking=True,
            detail=f"return type changed from {base.get('return', 'unannotated')} "
            f"to {head.get('return', 'unannotated')}",
        )
    _parameter_changes(differ, base["parameters"], head["parameters"])


# --- the whole snapshot -------------------------------------------------------


def diff(base: dict[str, Any], head: dict[str, Any]) -> list[Change]:
    """List every difference from base to head: tools, then public names and signatures."""
    differ = _Differ()
    for name in sorted(base["tools"].keys() | head["tools"].keys()):
        differ.subject = f"tool {name}"
        if name not in head["tools"]:
            differ.add(breaking=True, detail="removed")
        elif name not in base["tools"]:
            differ.add(breaking=False, detail="added")
        else:
            _tool_changes(differ, base["tools"][name], head["tools"][name])
    differ.subject = "permit_mcp.__all__"
    old_names, new_names = set(base["public"]["__all__"]), set(head["public"]["__all__"])
    for name in sorted(old_names - new_names):
        differ.add(breaking=True, detail=f"{name} removed")
    for name in sorted(new_names - old_names):
        differ.add(breaking=False, detail=f"{name} added")
    old_signatures, new_signatures = base["public"]["signatures"], head["public"]["signatures"]
    for name in sorted(old_signatures.keys() | new_signatures.keys()):
        differ.subject = f"signature {name}"
        if name not in new_signatures:
            differ.add(breaking=True, detail="removed")
        elif name not in old_signatures:
            differ.add(breaking=False, detail="added")
        else:
            _signature_changes(differ, old_signatures[name], new_signatures[name])
    return differ.changes


def load(path: Path) -> dict[str, Any]:
    """Read a snapshot and check its shape.

    Raises:
        OSError: The file cannot be read.
        ValueError: The file is not JSON (json.JSONDecodeError) or not a surface snapshot
            (NotASnapshotError).
    """
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(snapshot, dict):
        msg = "not a JSON object"
        raise NotASnapshotError(msg)
    tools, public = snapshot.get("tools"), snapshot.get("public")
    if not isinstance(tools, dict) or not tools:
        msg = 'no "tools" object with at least one tool'
        raise NotASnapshotError(msg)
    for name, tool in tools.items():
        if not isinstance(tool, dict) or not isinstance(tool.get("input_schema"), dict):
            msg = f'tool "{name}" has no input_schema object'
            raise NotASnapshotError(msg)
    if (
        not isinstance(public, dict)
        or not isinstance(public.get("__all__"), list)
        or not isinstance(public.get("signatures"), dict)
    ):
        msg = 'no "public" object with an "__all__" list and a "signatures" object'
        raise NotASnapshotError(msg)
    for name, signature in public["signatures"].items():
        parameters = signature.get("parameters") if isinstance(signature, dict) else None
        if not isinstance(parameters, list) or not all(
            isinstance(parameter, dict)
            and isinstance(parameter.get("name"), str)
            and parameter.get("kind") in _ALL_KINDS
            for parameter in parameters
        ):
            msg = f'signature "{name}" has no list of parameters with a name and a kind'
            raise NotASnapshotError(msg)
    return snapshot


def _values(encoded: set[str] | None) -> str:
    return ", ".join(sorted(encoded or set()))


def _show(value: Any) -> str:  # noqa: ANN401 - JSON
    return "none" if value is None else json.dumps(value)


def main(argv: list[str]) -> int:
    """Compare the snapshots named in argv, print the changes and return the exit status."""
    if len(argv) != 2:  # noqa: PLR2004 - BASE and HEAD
        print("surface_diff: DID NOT RUN: usage: surface_diff.py BASE HEAD")
        return DID_NOT_RUN
    snapshots = []
    for role, name in zip(("base", "head"), argv, strict=True):
        try:
            snapshots.append(load(Path(name)))
        except (OSError, ValueError) as exc:
            print(f"surface_diff: DID NOT RUN: {role} {name}: {exc}")
            return DID_NOT_RUN
    changes = diff(*snapshots)
    for change in changes:
        print(change)
    breaking = sum(change.breaking for change in changes)
    print(f"surface_diff: {len(changes)} changes, {breaking} breaking")
    return BREAKING if breaking else NO_BREAKING


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
