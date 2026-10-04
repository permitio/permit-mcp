#!/usr/bin/env python3
"""Find what moving a project from the permit-mcp 0.1 server to 1.0 touches.

Read-only and standard library only. It runs on Python 3.10 and later, the versions 0.1 ran
on, so it works before the project has moved. It reads the project's Python files with `ast`,
its dotenv files, MCP client configs (JSON), dependency files and other text files, and prints
one line per affected site:

    path:line: ID SAFETY message

ID names a section of SKILL.md. SAFE marks an edit that can be made as the message says;
NEEDS-REVIEW marks one that needs a decision, such as which Permit user the server acts as.

Usage:
    python3 scan.py PATH

PATH is a project directory, or one file such as a claude_desktop_config.json.

Exit status: 0 when nothing is found, 1 when something is, and 2 when PATH does not exist or a
file or directory could not be read or parsed. With 2, the findings are incomplete: stderr
names each file to review by hand.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator

SAFE = "SAFE"
REVIEW = "NEEDS-REVIEW"
MAX_BYTES = 5 * 1024 * 1024

RENAMED = {
    "RESOURCE_KEY": "PERMIT_RESOURCE",
    "TENANT": "PERMIT_TENANT",
    "ACCESS_ELEMENTS_CONFIG_ID": "PERMIT_ACCESS_REQUEST_ELEMENT",
    "OPERATION_ELEMENTS_CONFIG_ID": "PERMIT_OPERATION_APPROVAL_ELEMENT",
}
OLD_VARIABLES = (*RENAMED, "PROJECT_ID", "ENV_ID")
# The 0.1 tools. Each took a user_id argument.
OLD_TOOLS = frozenset(
    {
        "list_resource_instances",
        "create_access_request",
        "list_access_requests",
        "approve_access_request",
        "deny_access_request",
        "create_operation_approval",
        "list_operation_approvals",
        "approve_operation_approval",
        "deny_operation_approval",
    }
)
SKIPPED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "build",
        "dist",
        "site-packages",
        ".eggs",
    }
)
# This skill's own directory: its docs and this file quote the 0.1 patterns.
SKILL_DIR = Path(__file__).resolve().parent.parent

PROSE_SUFFIXES = frozenset({".md", ".txt"})
TEMPLATE_SUFFIXES = frozenset({".prompt", ".j2", ".jinja", ".jinja2", ".tmpl"})
CONFIG_SUFFIXES = frozenset({".sh", ".bash", ".zsh", ".yml", ".yaml", ".toml", ".ini", ".cfg"})

FASTMCP_MODULE = re.compile(r"mcp\.server\.fastmcp(?:\..*)?")
CLONE = re.compile(r"src[/\\]permit_mcp\b|permit_mcp[/\\]server\.py")
USER_ID = re.compile(r"\buser_id\b")
# Text counts as written for the model when it also talks about tools or functions; that
# leaves out SQL, log messages and API docs that name a user_id column or field.
FOR_THE_MODEL = re.compile(r"\b(?:tool|function)", re.IGNORECASE)
# The 0.1 names are generic. A file that sets the API key or names permit-mcp is a sign that
# they are the permit-mcp settings there.
PERMIT_CONTEXT = re.compile(r"\bPERMIT_API_KEY\s*[=:]|permit[-_]mcp", re.IGNORECASE)
DOTENV_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_]\w*)\s*=")
CONFIG_ASSIGNMENT = re.compile(r"(?<![\w${])(" + "|".join(OLD_VARIABLES) + r")(?=\s*[=:])")
PERMIT_MCP = re.compile(r"(?<![\w.-])permit[-_]mcp(?![\w-])", re.IGNORECASE)
OLD_PERMIT_MCP = re.compile(
    r"^\s*(?:\[[^\]]*\]\s*)?@"  # a direct reference: permit-mcp @ git+https://...
    r"|==\s*0\."
    r"|~=\s*0\."
    r"|<\s*1(?:\.0+)*(?![\d.])"
    r"|^\s*=\s*\{.*\b(?:path|git|url)\s*="  # a [tool.uv.sources] entry
)
MCP = re.compile(r"(?<![\w.-])mcp(?![\w.-])", re.IGNORECASE)
OLD_MCP = re.compile(r"==\s*1\.|~=\s*1\.|<=\s*1\.|<\s*2(?:\.0+)*(?![\d.])")

MESSAGES = {
    "C1-config": (
        "Runs the 0.1 server from a clone, with its settings in that clone's .env. Run "
        "`uvx permit-mcp` instead, with an env block that holds the 1.0 variables and "
        "PERMIT_MCP_USER."
    ),
    "C1": (
        "Runs the 0.1 server from a clone. Run `uvx permit-mcp` instead, with the 1.0 "
        "variables and PERMIT_MCP_USER in its environment."
    ),
    "D1": (
        "Requires permit-mcp 0.1 (a clone, a Git or path reference, or a version below 1.0). "
        "Require `permit-mcp>=1.0,<2` from PyPI."
    ),
    "D2": (
        "Keeps mcp below 2. permit-mcp 1.0 runs on mcp 2: require `mcp>=2.2.0,<3`, and move "
        "your own FastMCP code to MCPServer (see M2)."
    ),
    "E1-renamed": "{name} is {new} in 1.0; rename it (1.0 does not read {name}).",
    "E1-removed": (
        "{name} is removed in 1.0; delete it (the server reads the project and environment "
        "from the API key)."
    ),
    "E1-unsure": "If this belongs to permit-mcp: {message}",
    "E2": (
        "This .env sets 0.1 variables, and 1.0 does not load .env. If they are the permit-mcp "
        "settings, put them in the MCP client's env block, or run "
        "`uv run --env-file .env permit-mcp`."
    ),
    "E3": (
        "Runs permit-mcp without PERMIT_MCP_USER in its env block. If this is a 1.0 install, "
        "the command stops without it: set it to the key of the Permit user the server acts "
        "as. If it runs a 0.1 checkout, see C1."
    ),
    "M1-import": (
        "PermitServer is removed in 1.0. Import PermitTools and Settings, or create_server, "
        "from permit_mcp."
    ),
    "M1-call": (
        "PermitServer(mcp, exclude_tools=...) is removed in 1.0. Use "
        "PermitTools(settings, identity).register(server, exclude={{...}}) and "
        "`await tools.aclose()` in the server's lifespan, or "
        "create_server(settings, identity=..., exclude_tools=...)."
    ),
    "M2-import": (
        "mcp.server.fastmcp is gone in mcp 2, which 1.0 requires. Import MCPServer (was "
        "FastMCP) and Context from mcp.server.mcpserver, and ToolError from "
        "mcp.server.mcpserver.exceptions."
    ),
    "M2-call": (
        "FastMCP is MCPServer in mcp 2. Build MCPServer(name, lifespan=...) and register the "
        "Permit tools on it."
    ),
    "M3": (
        "A copy of the 0.1 server: this module is named permit_mcp and has PermitServer, so "
        "it shadows the installed permit-mcp 1.0. Delete it, or rename it, before installing "
        "1.0."
    ),
    "U1": (
        "Passes user_id to the Permit tool {tool}. 1.0 tools take no user argument; they act "
        "as the user bound in code. Remove it, and bind the user with bound_user, "
        "access_token_subject or your own identity resolver."
    ),
    "U2": (
        "Text for the model that mentions user_id. If it tells the model which user to pass "
        "to the Permit tools, remove that: 1.0 tools take no user argument, and the user is "
        "bound in code."
    ),
}


class Finding(NamedTuple):
    """One affected site."""

    path: str
    line: int
    change: str
    safety: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.change} {self.safety} {self.message}"


class UnreadableError(Exception):
    """A file the scanner needs could not be read or parsed."""


def finding(path: str, line: int, key: str, safety: str = REVIEW, **fields: str) -> Finding:
    """Build a finding whose ID is the part of `key` before any dash."""
    return Finding(path, line, key.partition("-")[0], safety, MESSAGES[key].format(**fields))


def variable_finding(path: str, line: int, name: str, *, sure: bool) -> Finding:
    """Report a 0.1 variable: SAFE where it is surely the permit-mcp setting."""
    key = "E1-renamed" if name in RENAMED else "E1-removed"
    message = MESSAGES[key].format(name=name, new=RENAMED.get(name, ""))
    if sure:
        return Finding(path, line, "E1", SAFE, message)
    return finding(path, line, "E1-unsure", message=message)


def dotted(node: ast.expr) -> str:
    """Return `a.b.c` for a Name or Attribute chain, and "" for anything else."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        receiver = dotted(node.value)
        return f"{receiver}.{node.attr}" if receiver else ""
    return ""


def vendored_copy_line(path: str, tree: ast.Module) -> int:
    """Return the line where a module named permit_mcp defines or imports PermitServer, or 0."""
    where = Path(path)
    if "permit_mcp" not in (where.stem, where.parent.name):
        return 0
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "PermitServer":
            return node.lineno
        if isinstance(node, ast.ImportFrom) and any(
            alias.name == "PermitServer" for alias in node.names
        ):
            return node.lineno
    return 0


class PythonScan:
    """The findings in one Python file."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.findings: list[Finding] = []
        self.permit_server_names: set[str] = set()
        self.fastmcp_names: set[str] = set()

    def add(self, line: int, key: str, **fields: str) -> None:
        """Record a NEEDS-REVIEW finding."""
        self.findings.append(finding(self.path, line, key, **fields))

    def run(self, tree: ast.Module) -> list[Finding]:
        """Scan the tree in one pass; calls are checked after it, once every import is known."""
        calls = []
        skipped: set[int] = set()  # docstrings, and the parts of f-strings
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                first = node.body[0] if node.body else None
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    skipped.add(id(first.value))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                self.check_import(node)
            elif isinstance(node, ast.Call):
                calls.append(node)
            elif isinstance(node, ast.Subscript) and re.fullmatch(
                r"(?:.*\.)?environ", dotted(node.value)
            ):
                self.check_variable(node.lineno, node.slice)
            elif isinstance(node, ast.JoinedStr) and id(node) not in skipped:
                skipped.update(id(part) for part in ast.walk(node))
                text = "".join(
                    str(part.value) if isinstance(part, ast.Constant) else "{}"
                    for part in node.values
                )
                self.check_text(node.lineno, text)
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in skipped
            ):
                self.check_text(node.lineno, node.value)
        for call in calls:
            self.check_call(call)
        return self.findings

    def check_import(self, node: ast.Import | ast.ImportFrom) -> None:
        """Report imports of PermitServer, from any module, and of mcp.server.fastmcp."""
        if isinstance(node, ast.Import):
            if any(FASTMCP_MODULE.fullmatch(alias.name) for alias in node.names):
                self.add(node.lineno, "M2-import")
            return
        module = node.module or ""
        names = {alias.name: alias.asname or alias.name for alias in node.names}
        if "PermitServer" in names:
            self.permit_server_names.add(names["PermitServer"])
            self.add(node.lineno, "M1-import")
        if FASTMCP_MODULE.fullmatch(module) or (module == "mcp.server" and "fastmcp" in names):
            if "FastMCP" in names:
                self.fastmcp_names.add(names["FastMCP"])
            self.add(node.lineno, "M2-import")

    def check_call(self, call: ast.Call) -> None:
        """Report PermitServer and FastMCP calls, user_id sent to a tool, and env reads."""
        target = dotted(call.func)
        name = target.rsplit(".", 1)[-1]
        if target in self.permit_server_names or name == "PermitServer":
            self.add(call.lineno, "M1-call")
        if target in self.fastmcp_names or target == "mcp.server.fastmcp.FastMCP":
            self.add(call.lineno, "M2-call")
        values = [*call.args, *(keyword.value for keyword in call.keywords)]
        tools = [name] + [value.value for value in values if isinstance(value, ast.Constant)]
        tool = next((str(tool) for tool in tools if tool in OLD_TOOLS), "")
        if tool:
            self.check_user_argument(call, values, tool)
        if name in ("getenv", "get", "pop", "setdefault") and call.args:
            self.check_variable(call.lineno, call.args[0])

    def check_variable(self, line: int, key: ast.expr) -> None:
        """Report code that reads a 0.1 variable, as in `os.environ["TENANT"]`."""
        if isinstance(key, ast.Constant) and key.value in OLD_VARIABLES:
            self.findings.append(variable_finding(self.path, line, key.value, sure=False))

    def check_user_argument(self, call: ast.Call, values: list[ast.expr], tool: str) -> None:
        """Report a user_id keyword or dict key in a call to a 0.1 tool."""
        lines = {keyword.value.lineno for keyword in call.keywords if keyword.arg == "user_id"}
        for value in values:
            for node in ast.walk(value):
                if isinstance(node, ast.Dict):
                    lines.update(
                        key.lineno
                        for key in node.keys
                        if isinstance(key, ast.Constant) and key.value == "user_id"
                    )
        for line in sorted(lines):
            self.add(line, "U1", tool=tool)

    def check_text(self, line: int, text: str) -> None:
        """Report a string that runs the 0.1 server, or prose for the model about user_id."""
        clone = CLONE.search(text)
        if clone:
            self.add(line + text.count("\n", 0, clone.start()), "C1")
        user_id = USER_ID.search(text)
        if user_id and FOR_THE_MODEL.search(text):
            self.add(line + text.count("\n", 0, user_id.start()), "U2")


def scan_python(path: str, text: str) -> list[Finding]:
    """Scan a Python file; a 0.1 copy of permit_mcp gets one M3 finding and nothing else."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as error:
        message = f"does not parse as Python: {error}"
        raise UnreadableError(message) from error
    vendored = vendored_copy_line(path, tree)
    if vendored:
        return [finding(path, vendored, "M3")]
    return PythonScan(path).run(tree)


def server_entries(
    node: object, keys: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], dict[str, object]]]:
    """Yield each object with a string `command`, with the keys that lead to it."""
    if isinstance(node, dict):
        if isinstance(node.get("command"), str):
            yield keys, node
        for key, value in node.items():
            yield from server_entries(value, (*keys, str(key)))
    elif isinstance(node, list):
        for item in node:
            yield from server_entries(item, keys)


def json_key_line(text: str, keys: tuple[str, ...]) -> int:
    """Return the line of the last of `keys`, finding each one after the one before."""
    position = 0
    found = 0
    for key in keys:
        match = re.compile(re.escape(json.dumps(key)) + r"\s*:").search(text, position)
        if match is None:
            break
        found = match.start()
        position = match.end()
    return text.count("\n", 0, found) + 1


def runs_old_server(words: list[str]) -> bool:
    """Return True for a command line that runs the 0.1 server.py from a clone."""
    return any(CLONE.search(word) for word in words) or (
        any(word.endswith("server.py") for word in words)
        and any("permit" in word for word in words)
    )


def runs_new_server(words: list[str]) -> bool:
    """Return True for a command line that runs the permit-mcp command or module."""
    for index, word in enumerate(words):
        name = word.replace("\\", "/").rsplit("/", 1)[-1]
        if re.fullmatch(r"permit-mcp(?:[@=<>~].*)?", name):
            return True
        if word == "permit_mcp" and index > 0 and words[index - 1] == "-m":
            return True
    return False


def scan_client_config(path: str, text: str) -> list[Finding]:
    """Scan a JSON file for MCP server entries that run permit-mcp."""
    if '"command"' not in text:
        return []
    try:
        config = json.loads(text)
    except ValueError as error:
        if "permit" in text.lower():
            message = f"is not valid JSON: {error}"
            raise UnreadableError(message) from error
        return []
    findings = []
    for keys, entry in server_entries(config):
        args = entry.get("args")
        words = [str(entry["command"])]
        if isinstance(args, list):
            words.extend(str(arg) for arg in args)
        old = runs_old_server(words)
        if not old and not runs_new_server(words):
            continue
        line = json_key_line(text, keys)
        env = entry.get("env")
        env = env if isinstance(env, dict) else {}
        if old:
            findings.append(finding(path, line, "C1-config"))
        elif "PERMIT_MCP_USER" not in env and "--env-file" not in words:
            findings.append(finding(path, line, "E3"))
        findings.extend(
            variable_finding(path, json_key_line(text, (*keys, "env", name)), name, sure=True)
            for name in env
            if name in OLD_VARIABLES
        )
    return findings


def specifiers(line: str, package: re.Pattern[str]) -> list[str]:
    """Return what follows each mention of a package on a line, up to a quote or a comment."""
    return [
        re.split(r"[\"'#]", line[match.end() :], maxsplit=1)[0] for match in package.finditer(line)
    ]


def requires_old_permit_mcp(line: str) -> bool:
    """Return True for a requirement on permit-mcp 0.1: a clone, a reference, or a 0.x pin."""
    found = specifiers(line, PERMIT_MCP)
    if found and (re.match(r"\s*-e\s", line) or re.search(r"\b(?:git\+|file:)", line)):
        return True
    return any(OLD_PERMIT_MCP.search(rest) for rest in found)


def scan_text(path: str, text: str) -> list[Finding]:
    """Scan a dotenv, dependency, config, prose or template file line by line."""
    name = Path(path).name
    suffix = Path(name).suffix
    dotenv = is_dotenv(name)
    dependencies = is_dependency_file(name)
    config = not dotenv and suffix not in PROSE_SUFFIXES | TEMPLATE_SUFFIXES
    sure = bool(PERMIT_CONTEXT.search(text))
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        if CLONE.search(line):
            findings.append(finding(path, number, "C1"))
        if dependencies and requires_old_permit_mcp(line):
            findings.append(finding(path, number, "D1", SAFE))
        if dependencies and any(OLD_MCP.search(rest) for rest in specifiers(line, MCP)):
            findings.append(finding(path, number, "D2"))
        if USER_ID.search(line) and (
            suffix in TEMPLATE_SUFFIXES or (suffix in PROSE_SUFFIXES and FOR_THE_MODEL.search(line))
        ):
            findings.append(finding(path, number, "U2"))
        assignment = DOTENV_ASSIGNMENT.match(line) if dotenv else None
        if assignment and assignment.group(1) in OLD_VARIABLES:
            findings.append(variable_finding(path, number, assignment.group(1), sure=sure))
        if config:
            findings.extend(
                variable_finding(path, number, match.group(1), sure=sure)
                for match in CONFIG_ASSIGNMENT.finditer(line)
            )
    variables = [item.line for item in findings if item.change == "E1"]
    if name == ".env" and variables:
        findings.append(finding(path, variables[0], "E2"))
    return findings


def is_dotenv(name: str) -> bool:
    """Return True for `.env`, `.env.local`, `prod.env`, `.envrc` and the like."""
    return name in (".env", ".envrc") or name.startswith(".env.") or name.endswith(".env")


def is_dependency_file(name: str) -> bool:
    """Return True for pyproject.toml and requirements files."""
    return name == "pyproject.toml" or (
        name.startswith("requirements") and name.endswith((".txt", ".in"))
    )


def is_scanned(name: str) -> bool:
    """Return True for the files a directory scan reads."""
    suffix = Path(name).suffix
    return (
        suffix in (".py", ".json")
        or suffix in PROSE_SUFFIXES | TEMPLATE_SUFFIXES | CONFIG_SUFFIXES
        or is_dotenv(name)
        or is_dependency_file(name)
        or name.startswith("Dockerfile")
    )


def read(path: Path) -> str:
    """Read a regular file of at most MAX_BYTES, following symbolic links."""
    if not stat.S_ISREG(path.stat().st_mode):
        message = "is not a regular file"
        raise UnreadableError(message)
    with path.open("rb") as file:
        data = file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        message = f"is larger than {MAX_BYTES // (1024 * 1024)} MB"
        raise UnreadableError(message)
    return data.decode("utf-8", errors="replace")


def scan_path(path: Path, shown: str) -> tuple[list[Finding], str]:
    """Read and scan one file; return its findings and a problem line ("" for none)."""
    try:
        text = read(path)
        if path.suffix == ".py":
            return scan_python(shown, text), ""
        if path.suffix == ".json":
            return scan_client_config(shown, text), ""
        return scan_text(shown, text), ""
    except OSError as error:
        return [], f"{shown}: could not be read: {error.strerror or error}"
    except UnreadableError as error:
        return [], f"{shown}: {error}"


def is_environment(directory: Path) -> bool:
    """Return True for a virtual environment: a directory with a pyvenv.cfg."""
    try:
        return (directory / "pyvenv.cfg").exists()
    except OSError:
        return False  # os.walk reports the directory it cannot list


def project_files(root: Path, problems: list[str]) -> Iterator[Path]:
    """Yield the files a scan reads; add each directory it cannot list to `problems`."""

    def unreadable(error: OSError) -> None:
        shown = Path(error.filename or root).relative_to(root).as_posix()
        problems.append(f"{shown}: could not be read: {error.strerror or error}")

    for directory, subdirectories, files in os.walk(root, onerror=unreadable):
        here = Path(directory)
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if name not in SKIPPED_DIRS
            and not name.endswith(".egg-info")
            and not is_environment(here / name)
            and (here / name).resolve() != SKILL_DIR
        )
        for name in sorted(files):
            if is_scanned(name):
                yield here / name


def scan(target: Path) -> tuple[list[Finding], list[str]]:
    """Scan a directory or one file; return the sorted findings and the problem lines."""
    problems: list[str] = []
    if target.is_dir():
        files = [
            (path, path.relative_to(target).as_posix()) for path in project_files(target, problems)
        ]
    else:
        files = [(target, target.name)]
    findings: set[Finding] = set()
    for path, shown in files:
        found, problem = scan_path(path, shown)
        findings.update(found)
        if problem:
            problems.append(problem)
    return sorted(findings, key=lambda item: (item.path, item.line, item.change)), problems


def main(argv: list[str] | None = None) -> int:
    """Run the scanner and return its exit status."""
    parser = argparse.ArgumentParser(
        description="Find what moving a project from permit-mcp 0.1 to 1.0 touches."
    )
    parser.add_argument("path", type=Path, help="a project directory, or one file")
    target = parser.parse_args(argv).path
    if not target.exists():
        print(f"scan.py: {target}: no such file or directory", file=sys.stderr)
        return 2
    findings, problems = scan(target)
    for item in findings:
        print(item)
    for problem in problems:
        print(f"scan.py: {problem}; review it by hand", file=sys.stderr)
    if problems:
        return 2
    if findings:
        return 1
    print("No permit-mcp 0.1 usage found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
