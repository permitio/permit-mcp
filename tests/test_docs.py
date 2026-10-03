"""The README and the upgrade guide match the code.

The README's tools table matches the registered tools, its configuration table matches the
settings `Settings.from_env` reads and their defaults, and every Python block of both documents
type-checks.
"""

from __future__ import annotations

import dataclasses
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from mcp.client import Client

from permit_mcp import TOOL_NAMES, Settings, create_server
from permit_mcp.config import ENV_VARS

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
UPGRADE_GUIDE = ROOT / "docs" / "upgrade-to-1.0.md"
TOOLS_HEADER = "| Tool | Description |"
CONFIG_HEADER = "| Variable | Default | Meaning |"
# The settings the README's tool descriptions are shown with.
README_SETTINGS = {"resource": "documents", "tenant": "default"}
SENTENCE_END = re.compile(r"(?<=\.)\s+")
CODE_CELL = re.compile(r"`([^`]+)`")
REQUIRED = "required"
UNSET = "unset"
# Put on the line before a Python block that shows 0.1 code, which no longer type-checks.
SKIP_MARKER = "<!-- docs-check: skip, 0.1 code -->"
PYTHON_BLOCK = re.compile(r"^```python\n(.*?)^```$", re.MULTILINE | re.DOTALL)
# Strict, as for the package. Examples may leave a body as `...`, and the Permit SDK that the
# README's best-practice examples use is not a dependency.
MYPY_CONFIG = """\
[mypy]
python_version = 3.11
strict = True
warn_unreachable = True
disable_error_code = empty-body

[mypy-permit.*]
ignore_missing_imports = True
"""


def table_rows(text: str, header: str) -> list[list[str]]:
    """Return the cells of each row of the table that starts with `header`."""
    lines = text.splitlines()
    assert header in lines, f"README.md has no table with the header {header!r}"
    start = lines.index(header) + 2  # the header and its separator row
    rows: list[list[str]] = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def code_name(cell: str) -> str:
    """Return the name in a cell that holds only a `code` span."""
    match = CODE_CELL.fullmatch(cell)
    assert match is not None, f"expected a cell with one `code` span, found {cell!r}"
    return match.group(1)


def first_sentence(description: str) -> str:
    return SENTENCE_END.split(description, maxsplit=1)[0]


def tool_table_problems(text: str, descriptions: Mapping[str, str]) -> list[str]:
    """Compare the tools table with the registered tools' descriptions, in TOOL_NAMES order."""
    rows = table_rows(text, TOOLS_HEADER)
    names = [code_name(row[0]) for row in rows]
    if names != list(TOOL_NAMES):
        return [f"the tools table lists {names}, expected {list(TOOL_NAMES)}"]
    return [
        f"{name}: the README says {row[1]!r}, the tool says {first_sentence(descriptions[name])!r}"
        for name, row in zip(names, rows, strict=True)
        if row[1] != first_sentence(descriptions[name])
    ]


def setting_defaults() -> dict[str, object]:
    """Map each environment variable to the default of its Settings field."""
    fields = {field.name: field for field in dataclasses.fields(Settings)}
    return {variable: fields[name].default for name, variable in ENV_VARS.items()}


def default_cell(default: object) -> str:
    """Return the Default cell for a field default: required, unset, or the value as code."""
    if default is dataclasses.MISSING:
        return REQUIRED
    if default is None:
        return UNSET
    return f"`{default}`"


def config_table_problems(text: str, defaults: Mapping[str, object]) -> list[str]:
    """Compare the configuration table with the variables Settings reads and their defaults."""
    rows = table_rows(text, CONFIG_HEADER)
    variables = [code_name(row[0]) for row in rows]
    problems: list[str] = []
    repeated = sorted({name for name in variables if variables.count(name) > 1})
    if repeated:
        problems.append(f"listed more than once: {repeated}")
    missing = sorted(set(defaults) - set(variables))
    if missing:
        problems.append(f"missing: {missing}")
    extra = sorted(set(variables) - set(defaults))
    if extra:
        problems.append(f"not read by Settings.from_env: {extra}")
    for variable, row in zip(variables, rows, strict=True):
        expected = default_cell(defaults[variable]) if variable in defaults else row[1]
        if row[1] != expected:
            problems.append(
                f"{variable}: the README's default is {row[1]!r}, expected {expected!r}"
            )
    return problems


def python_blocks(text: str) -> list[str]:
    """Return the code of each Python block in `text`, leaving out those marked as 0.1 code."""
    blocks: list[str] = []
    for match in PYTHON_BLOCK.finditer(text):
        line_before = text[: match.start()].rstrip("\n").rpartition("\n")[2]
        if line_before != SKIP_MARKER:
            blocks.append(match.group(1))
    return blocks


def run_mypy(blocks: Mapping[str, str], directory: Path, cache: Path) -> tuple[int, str]:
    """Type-check each block as a module named by its key; return mypy's status and output."""
    directory.mkdir(parents=True, exist_ok=True)
    config = directory / "mypy.ini"
    config.write_text(MYPY_CONFIG, encoding="utf-8")
    files: list[str] = []
    for name, code in blocks.items():
        path = directory / f"{name}.py"
        path.write_text(code, encoding="utf-8")
        files.append(str(path))
    result = subprocess.run(  # noqa: S603 - this interpreter's mypy on files the test wrote
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(config),
            "--cache-dir",
            str(cache),
            *files,
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=directory,
    )
    return result.returncode, result.stdout + result.stderr


async def registered_descriptions() -> dict[str, str]:
    settings = Settings(
        api_key="docs-key",
        access_request_element="access-element",
        operation_approval_element="approval-element",
        user="alice",
        **README_SETTINGS,
    )
    async with Client(create_server(settings)) as client:
        tools = (await client.list_tools()).tools
    return {tool.name: tool.description or "" for tool in tools}


@pytest.fixture
def readme() -> str:
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def mypy_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One mypy cache for the session, so the second run reuses the first one's work."""
    return tmp_path_factory.mktemp("mypy-cache")


async def test_tools_table_matches_the_registered_tools(readme: str) -> None:
    assert tool_table_problems(readme, await registered_descriptions()) == []


def test_config_table_matches_the_settings(readme: str) -> None:
    assert config_table_problems(readme, setting_defaults()) == []


def test_documented_python_type_checks(tmp_path: Path, mypy_cache: Path) -> None:
    blocks: dict[str, str] = {}
    for prefix, path in (("readme", README), ("upgrade", UPGRADE_GUIDE)):
        found = python_blocks(path.read_text(encoding="utf-8"))
        assert found, f"no Python block found in {path.name}"
        blocks.update({f"{prefix}_{index}": code for index, code in enumerate(found)})
    status, output = run_mypy(blocks, tmp_path, mypy_cache)
    assert status == 0, output


def _swap_first_two_tool_rows(text: str) -> str:
    lines = text.splitlines()
    first = lines.index(TOOLS_HEADER) + 2
    lines[first], lines[first + 1] = lines[first + 1], lines[first]
    return "\n".join(lines)


def _drop_line(text: str, prefix: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.startswith(prefix))


TOOL_TABLE_BREAKS: dict[str, Callable[[str], str]] = {
    "row-missing": lambda text: _drop_line(text, "| `cancel_operation_approval` |"),
    "rows-out-of-order": _swap_first_two_tool_rows,
    "description-changed": lambda text: text.replace(
        "| Deny an access request. |", "| Deny an access request now. |"
    ),
    "unknown-tool": lambda text: text.replace("| `deny_access_request` |", "| `deny_request` |"),
}


@pytest.mark.parametrize("name", sorted(TOOL_TABLE_BREAKS))
async def test_tools_check_fails_when_the_readme_is_wrong(readme: str, name: str) -> None:
    broken = TOOL_TABLE_BREAKS[name](readme)
    assert broken != readme
    assert tool_table_problems(broken, await registered_descriptions()) != []


async def test_tools_check_fails_when_a_tool_description_changes(readme: str) -> None:
    descriptions = await registered_descriptions()
    descriptions["deny_access_request"] = "Reject an access request."
    assert tool_table_problems(readme, descriptions) != []


CONFIG_TABLE_BREAKS: dict[str, Callable[[str], str]] = {
    "variable-missing": lambda text: _drop_line(text, "| `PERMIT_PDP_URL` |"),
    "variable-unknown": lambda text: text.replace(
        "| `PERMIT_TENANT` | `default` |",
        "| `PERMIT_TENANT` | `default` |\n| `TENANT` | unset |",
    ),
    "variable-repeated": lambda text: text.replace(
        "| `PERMIT_TENANT` | `default` |",
        "| `PERMIT_TENANT` | `default` | x |\n| `PERMIT_TENANT` | `default` |",
    ),
    "default-changed": lambda text: text.replace(
        "| `PERMIT_TENANT` | `default` |", "| `PERMIT_TENANT` | `main` |"
    ),
    "required-shown-as-unset": lambda text: text.replace(
        "| `PERMIT_RESOURCE` | required |", "| `PERMIT_RESOURCE` | unset |"
    ),
}


@pytest.mark.parametrize("name", sorted(CONFIG_TABLE_BREAKS))
def test_config_check_fails_when_the_readme_is_wrong(readme: str, name: str) -> None:
    broken = CONFIG_TABLE_BREAKS[name](readme)
    assert broken != readme
    assert config_table_problems(broken, setting_defaults()) != []


DEFAULT_CHANGES: dict[str, object] = {
    "PERMIT_TENANT": "main",
    "PERMIT_PDP_URL": "http://localhost:7766",
    "PERMIT_MCP_USER": dataclasses.MISSING,
    "PERMIT_API_KEY": None,
}


@pytest.mark.parametrize("variable", sorted(DEFAULT_CHANGES))
def test_config_check_fails_when_a_default_changes(readme: str, variable: str) -> None:
    defaults = {**setting_defaults(), variable: DEFAULT_CHANGES[variable]}
    assert config_table_problems(readme, defaults) != []


@pytest.mark.parametrize("header", [TOOLS_HEADER, CONFIG_HEADER])
def test_missing_table_fails(readme: str, header: str) -> None:
    with pytest.raises(AssertionError, match="no table"):
        table_rows(readme.replace(header, "| Something | Else |"), header)


def test_first_sentence_ends_at_the_first_full_stop() -> None:
    assert first_sentence("Cancel it (the ID, not the key). Permit refuses.") == (
        "Cancel it (the ID, not the key)."
    )


def test_marked_blocks_are_left_out() -> None:
    text = (
        "```python\nchecked = 1\n```\n\n"
        f"{SKIP_MARKER}\n```python\nold = 1\n```\n\n"
        "```json\n{}\n```\n"
    )
    assert python_blocks(text) == ["checked = 1\n"]


def test_the_upgrade_guide_leaves_out_only_its_0_1_example() -> None:
    text = UPGRADE_GUIDE.read_text(encoding="utf-8")
    blocks = python_blocks(text)
    assert len(blocks) == len(PYTHON_BLOCK.findall(text)) - 1
    assert not any("mcp.server.fastmcp" in block for block in blocks)


def test_type_check_fails_on_a_wrong_block(tmp_path: Path, mypy_cache: Path) -> None:
    wrong = "from permit_mcp import create_server\n\nserver = create_server(exclude=['x'])\n"
    status, output = run_mypy({"wrong": wrong}, tmp_path, mypy_cache)
    assert status != 0
    assert "wrong.py" in output
