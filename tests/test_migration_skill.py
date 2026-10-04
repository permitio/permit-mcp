"""The permit-mcp-1-migration skill: its scanner, run on sample projects, and its SKILL.md.

The scanner runs the way an agent runs it: as a script, by path, in a subprocess. The sample
projects in fixtures/migration are a 0.1 project (v0_1) and the same project on 1.0 (v1_0).
Their files are stored with a `.fixture` suffix, and `sample_project()` restores the real
names in a temporary copy: under their real names, ruff and mypy would check the 0.1 code,
check-json would read the configs, and .gitignore would leave out the `.env`.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = ROOT / "skills" / "permit-mcp-1-migration"
SCANNER = SKILL_DIR / "scripts" / "scan.py"
SKILL = SKILL_DIR / "SKILL.md"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "migration"
TIMEOUT_SECONDS = 60

SAFE = "SAFE"
REVIEW = "NEEDS-REVIEW"
CLEAN = "No permit-mcp 0.1 usage found.\n"
FINDING = re.compile(
    r"(?P<path>[^:]+):(?P<line>\d+): (?P<id>[A-Z]\d) (?P<safety>SAFE|NEEDS-REVIEW) (?P<message>.+)"
)

Finding = tuple[str, int, str, str]

V0_1_FINDINGS: list[Finding] = [
    (".env", 1, "E1", SAFE),
    (".env", 1, "E2", REVIEW),
    (".env", 2, "E1", SAFE),
    (".env", 5, "E1", SAFE),
    (".env", 6, "E1", SAFE),
    (".env", 7, "E1", SAFE),
    (".env", 8, "E1", SAFE),
    ("README.md", 5, "C1", REVIEW),
    ("README.md", 7, "U2", REVIEW),
    ("app/chat.py", 7, "C1", REVIEW),
    ("app/chat.py", 13, "U2", REVIEW),
    ("app/chat.py", 22, "U1", REVIEW),
    ("app/permit_mcp.py", 10, "M3", REVIEW),
    ("app/server.py", 5, "M2", REVIEW),
    ("app/server.py", 6, "M2", REVIEW),
    ("app/server.py", 7, "M1", REVIEW),
    ("app/server.py", 9, "E1", REVIEW),
    ("app/server.py", 10, "E1", REVIEW),
    ("app/server.py", 12, "M2", REVIEW),
    ("app/server.py", 13, "M1", REVIEW),
    ("claude_desktop_config.json", 7, "C1", REVIEW),
    ("claude_desktop_config.json", 16, "E1", SAFE),
    ("pyproject.toml", 6, "D2", REVIEW),
    ("pyproject.toml", 7, "D1", SAFE),
]


def sample_project(name: str, destination: Path) -> Path:
    """Copy a sample project to destination, with the real file names."""
    root = destination / name
    shutil.copytree(FIXTURES / name, root)
    for path in sorted(root.rglob("*.fixture")):
        path.rename(path.with_suffix(""))
    return root


def write(root: Path, files: dict[str, str]) -> Path:
    """Write files under root; the contents are dedented."""
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content))
    return root


def run_scanner(*arguments: str | Path) -> subprocess.CompletedProcess[str]:
    """Run the scanner script by path, as SKILL.md tells an agent to."""
    # The command is this interpreter and the scanner in this repository.
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SCANNER), *(str(argument) for argument in arguments)],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


def split(line: str) -> tuple[Finding, str]:
    """Return (path, line, id, safety) and the message of one finding line."""
    match = FINDING.fullmatch(line)
    assert match, f"not a finding line: {line!r}"
    return (match["path"], int(match["line"]), match["id"], match["safety"]), match["message"]


def parse(stdout: str) -> list[Finding]:
    """Return (path, line, id, safety) for each line the scanner printed."""
    return [split(line)[0] for line in stdout.splitlines()]


def findings(root: Path) -> list[Finding]:
    """Scan root, check the exit status agrees with the result, and return the findings."""
    result = run_scanner(root)
    assert result.stderr == ""
    if result.returncode == 0:
        assert result.stdout == CLEAN
        return []
    assert result.returncode == 1, result.stdout
    return parse(result.stdout)


def messages(root: Path) -> dict[Finding, str]:
    """Scan root and return each finding's message."""
    return dict(split(line) for line in run_scanner(root).stdout.splitlines())


# ---------------------------------------------------------------------------
# The sample projects
# ---------------------------------------------------------------------------


def test_0_1_project_reports_every_site_and_exits_1(tmp_path: Path) -> None:
    result = run_scanner(sample_project("v0_1", tmp_path))

    assert result.returncode == 1
    assert result.stderr == ""
    assert parse(result.stdout) == V0_1_FINDINGS


def test_1_0_project_is_clean_and_exits_0(tmp_path: Path) -> None:
    result = run_scanner(sample_project("v1_0", tmp_path))

    assert (result.returncode, result.stdout, result.stderr) == (0, CLEAN, "")


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (1, "TENANT is PERMIT_TENANT in 1.0; rename it (1.0 does not read TENANT)."),
        (2, "RESOURCE_KEY is PERMIT_RESOURCE in 1.0; rename it"),
        (5, "PROJECT_ID is removed in 1.0; delete it (the server reads the project and"),
        (6, "ENV_ID is removed in 1.0; delete it"),
        (7, "ACCESS_ELEMENTS_CONFIG_ID is PERMIT_ACCESS_REQUEST_ELEMENT in 1.0;"),
        (8, "OPERATION_ELEMENTS_CONFIG_ID is PERMIT_OPERATION_APPROVAL_ELEMENT in 1.0;"),
    ],
)
def test_each_variable_finding_names_its_fix(tmp_path: Path, line: int, expected: str) -> None:
    stdout = run_scanner(sample_project("v0_1", tmp_path)).stdout

    assert f".env:{line}: E1 SAFE {expected}" in stdout


def test_messages_name_the_1_0_replacements(tmp_path: Path) -> None:
    message = messages(sample_project("v0_1", tmp_path))

    assert "uv run --env-file .env permit-mcp" in message[".env", 1, "E2", REVIEW]
    config = message["claude_desktop_config.json", 7, "C1", REVIEW]
    assert "uvx permit-mcp" in config
    assert "PERMIT_MCP_USER" in config
    permit_server = message["app/server.py", 13, "M1", REVIEW]
    assert "PermitTools(settings, identity).register(server, exclude={...})" in permit_server
    assert "aclose()" in permit_server
    assert "create_server(" in permit_server
    assert "mcp.server.mcpserver" in message["app/server.py", 5, "M2", REVIEW]
    assert message["app/server.py", 9, "E1", REVIEW].startswith(
        "If this belongs to permit-mcp: TENANT is PERMIT_TENANT in 1.0"
    )
    assert (
        "Delete it, or rename it, before installing"
        in message["app/permit_mcp.py", 10, "M3", REVIEW]
    )
    assert "Permit tool create_access_request" in message["app/chat.py", 22, "U1", REVIEW]
    assert "bound_user" in message["app/chat.py", 22, "U1", REVIEW]
    assert "permit-mcp>=1.0,<2" in message["pyproject.toml", 7, "D1", SAFE]
    assert "mcp>=2.2.0,<3" in message["pyproject.toml", 6, "D2", REVIEW]


def test_scanner_does_not_modify_the_project(tmp_path: Path) -> None:
    root = sample_project("v0_1", tmp_path)

    def digest() -> dict[str, str]:
        return {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    before = digest()
    assert run_scanner(root).returncode == 1
    assert digest() == before


def test_one_file_can_be_scanned_on_its_own(tmp_path: Path) -> None:
    config = sample_project("v0_1", tmp_path) / "claude_desktop_config.json"

    assert findings(config) == [
        ("claude_desktop_config.json", 7, "C1", REVIEW),
        ("claude_desktop_config.json", 16, "E1", SAFE),
    ]


# ---------------------------------------------------------------------------
# Exit status 2: the scan is incomplete
# ---------------------------------------------------------------------------


def test_missing_path_exits_2(tmp_path: Path) -> None:
    result = run_scanner(tmp_path / "missing")

    assert result.returncode == 2
    assert result.stdout == ""
    assert "missing: no such file or directory" in result.stderr


def test_no_path_exits_2() -> None:
    result = run_scanner()

    assert result.returncode == 2
    assert result.stdout == ""
    assert "usage:" in result.stderr


def test_python_that_does_not_parse_exits_2_and_still_reports_the_rest(tmp_path: Path) -> None:
    write(tmp_path, {"broken.py": "def (:\n", ".env": "TENANT=default\nPERMIT_API_KEY=x\n"})

    result = run_scanner(tmp_path)

    assert result.returncode == 2
    assert parse(result.stdout) == [(".env", 1, "E1", SAFE), (".env", 1, "E2", REVIEW)]
    assert "broken.py: does not parse as Python" in result.stderr
    assert "review it by hand" in result.stderr


def test_invalid_json_that_mentions_permit_exits_2(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            ".vscode/mcp.json": """\
                {
                  // the Permit server
                  "servers": {"permit": {"command": "uvx", "args": ["permit-mcp"]}},
                }
            """
        },
    )

    result = run_scanner(tmp_path)

    assert result.returncode == 2
    assert result.stdout == ""
    assert ".vscode/mcp.json: is not valid JSON" in result.stderr


def test_invalid_json_without_a_permit_server_is_not_a_problem(tmp_path: Path) -> None:
    write(
        tmp_path,
        {".vscode/tasks.json": '{\n  // build\n  "tasks": [{"command": "make"}],\n}\n'},
    )

    assert findings(tmp_path) == []


@pytest.mark.parametrize("unreadable", ["file", "directory"])
def test_an_unreadable_file_or_directory_exits_2(tmp_path: Path, unreadable: str) -> None:
    write(tmp_path, {"app/host.py": "x = 1\n", ".env": "TENANT=default\nPERMIT_API_KEY=x\n"})
    locked = tmp_path / "app" / ("host.py" if unreadable == "file" else "")
    locked.chmod(0)
    try:
        result = run_scanner(tmp_path)
    finally:
        locked.chmod(0o755)

    assert result.returncode == 2
    assert parse(result.stdout) == [(".env", 1, "E1", SAFE), (".env", 1, "E2", REVIEW)]
    shown = "app/host.py" if unreadable == "file" else "app"
    assert f"scan.py: {shown}: could not be read: Permission denied; review it" in result.stderr


def test_a_fifo_is_reported_without_being_opened(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / ".env")

    result = run_scanner(tmp_path)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "scan.py: .env: is not a regular file" in result.stderr


def test_a_link_to_a_device_is_reported_without_being_read(tmp_path: Path) -> None:
    (tmp_path / "zero.py").symlink_to("/dev/zero")

    result = run_scanner(tmp_path)

    assert result.returncode == 2
    assert "scan.py: zero.py: is not a regular file" in result.stderr


def test_a_file_over_5_mb_is_reported_and_not_scanned(tmp_path: Path) -> None:
    (tmp_path / "big.py").write_text("x = 1\n" * 1_000_000)

    result = run_scanner(tmp_path)

    assert result.returncode == 2
    assert "scan.py: big.py: is larger than 5 MB" in result.stderr


def test_a_link_to_a_small_file_is_scanned(tmp_path: Path) -> None:
    write(tmp_path, {"real/.env": "TENANT=default\nPERMIT_API_KEY=x\n"})
    (tmp_path / ".env").symlink_to(tmp_path / "real" / ".env")

    assert findings(tmp_path) == [
        (".env", 1, "E1", SAFE),
        (".env", 1, "E2", REVIEW),
        ("real/.env", 1, "E1", SAFE),
        ("real/.env", 1, "E2", REVIEW),
    ]


# ---------------------------------------------------------------------------
# Single rules
# ---------------------------------------------------------------------------


def test_client_config_entries(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "mcp.json": """\
                {
                  "mcpServers": {
                    "no-user": {
                      "command": "uvx",
                      "args": ["permit-mcp@1.0.0"],
                      "env": {"PERMIT_RESOURCE": "documents", "RESOURCE_KEY": "documents"}
                    },
                    "env-file": {
                      "command": "uv",
                      "args": ["run", "--env-file", ".env", "permit-mcp"]
                    },
                    "from-a-clone": {
                      "command": "uv",
                      "args": ["run", "--directory", "/ABSOLUTE/PATH/TO/permit-mcp", "permit-mcp"],
                      "env": {"PERMIT_MCP_USER": "alice"}
                    },
                    "checkout-python": {
                      "command": "/ABSOLUTE/PATH/TO/permit-mcp/.venv/bin/python",
                      "args": ["-m", "permit_mcp"]
                    },
                    "clone-root": {
                      "command": "uv",
                      "args": ["--directory", "/ABSOLUTE/PATH/TO/permit-mcp", "run", "server.py"]
                    },
                    "other": {"command": "npx", "args": ["server.py"], "env": {"TENANT": "x"}}
                  }
                }
            """
        },
    )

    found = messages(tmp_path)

    assert list(found) == [
        ("mcp.json", 3, "E3", REVIEW),
        ("mcp.json", 6, "E1", SAFE),
        ("mcp.json", 17, "E3", REVIEW),
        ("mcp.json", 21, "C1", REVIEW),
    ]
    assert "If this is a 1.0 install" in found["mcp.json", 17, "E3", REVIEW]
    assert "If it runs a 0.1 checkout, see C1" in found["mcp.json", 17, "E3", REVIEW]


def test_imports_and_calls_are_traced_through_aliases(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "host.py": """\
                import mcp.server.fastmcp
                from mcp.server.fastmcp import FastMCP as Server
                from permit_mcp.server import PermitServer as Tools

                server = Server("host")
                Tools(server)
                mcp.server.fastmcp.FastMCP("other")
            """,
            "app/main.py": """\
                def build(mcp):
                    return PS(mcp)

                from server import PermitServer as PS
            """,
        },
    )

    assert findings(tmp_path) == [
        ("app/main.py", 2, "M1", REVIEW),
        ("app/main.py", 4, "M1", REVIEW),
        ("host.py", 1, "M2", REVIEW),
        ("host.py", 2, "M2", REVIEW),
        ("host.py", 3, "M1", REVIEW),
        ("host.py", 5, "M2", REVIEW),
        ("host.py", 6, "M1", REVIEW),
        ("host.py", 7, "M2", REVIEW),
    ]


def test_standalone_fastmcp_package_is_not_reported(tmp_path: Path) -> None:
    write(tmp_path, {"host.py": 'from fastmcp import FastMCP\n\nserver = FastMCP("host")\n'})

    assert findings(tmp_path) == []


def test_a_vendored_0_1_copy_is_reported_once(tmp_path: Path) -> None:
    server = "from mcp.server.fastmcp import FastMCP\n\n\nclass PermitServer:\n    pass\n"
    write(
        tmp_path,
        {
            "vendor/permit_mcp/server.py": server,
            "vendor/permit_mcp/__init__.py": "from .server import PermitServer\n",
            "permit_mcp.py": server,
            "permit_server.py": server,
        },
    )

    assert findings(tmp_path) == [
        ("permit_mcp.py", 4, "M3", REVIEW),
        ("permit_server.py", 1, "M2", REVIEW),
        ("vendor/permit_mcp/__init__.py", 1, "M3", REVIEW),
        ("vendor/permit_mcp/server.py", 4, "M3", REVIEW),
    ]


def test_user_id_sent_to_a_tool_by_keyword_or_in_arguments(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "client.py": """\
                async def run(session, permit_server, user, u):
                    await permit_server.approve_access_request(
                        user_id=user, access_request_id="2c1b3c7e-0f0b-4c4e-9d43-8f3d1b2b7a10"
                    )
                    await session.call_tool(name="list_access_requests", arguments={"user_id": u})
                    await session.call_tool("my_own_tool", {"user_id": user})
                    return {"user_id": user}
            """
        },
    )

    assert findings(tmp_path) == [("client.py", 3, "U1", REVIEW), ("client.py", 5, "U1", REVIEW)]


def test_prose_for_the_model_is_reported_and_other_text_is_not(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "prompts.py": '''\
                """Prompts. The tools used to take a user_id; this docstring is not reported."""

                SYSTEM = (
                    "You order food.\\n"
                    "Pass user_id 42 to every function you call.\\n"
                )
                QUERY = "SELECT name FROM users WHERE user_id = ?"
                LOG = "no user_id in the request"
            ''',
            "prompts/system.j2": "Hello.\nThe user_id is {{ user.id }}.\n",
            "docs/api.md": "# API\n\n`GET /users/{user_id}` returns one user.\n",
            "docs/agent.md": "Give the tools the user_id of the caller.\n",
            "notes.txt": "Each order row has a user_id column.\n",
        },
    )

    assert findings(tmp_path) == [
        ("docs/agent.md", 1, "U2", REVIEW),
        ("prompts.py", 5, "U2", REVIEW),
        ("prompts/system.j2", 2, "U2", REVIEW),
    ]


def test_environment_reads_in_code(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "settings.py": """\
                import os
                from os import environ, getenv

                project = os.environ.get("PROJECT_ID")
                os.environ["ENV_ID"] = "x"
                tenant = getenv("TENANT")
                element = environ["ACCESS_ELEMENTS_CONFIG_ID"]
                resource = os.getenv("PERMIT_RESOURCE")
                unrelated = {"TENANT": 1}["TENANT"]
            """
        },
    )

    assert findings(tmp_path) == [
        ("settings.py", 4, "E1", REVIEW),
        ("settings.py", 5, "E1", REVIEW),
        ("settings.py", 6, "E1", REVIEW),
        ("settings.py", 7, "E1", REVIEW),
    ]


def test_variables_are_safe_only_where_permit_mcp_is_named(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "compose.yaml": """\
                services:
                  permit:
                    image: ghcr.io/example/permit-mcp
                    environment:
                      RESOURCE_KEY: documents
                      PERMIT_TENANT: default
                      - TENANT=default
            """,
            ".env.local": "PERMIT_API_KEY=x\nexport PROJECT_ID=example\n",
            "Dockerfile": "FROM python:3.13\nENV PROJECT_ID=example\n",
            "deploy.sh": """\
                export TENANT=acme
                echo "$TENANT" $TENANT
                region=${TENANT:-x}
            """,
            ".github/workflows/deploy.yml": """\
                jobs:
                  deploy:
                    env:
                      RESOURCE_KEY: invoices
            """,
            ".envrc": "export ENV_ID=staging\n",
            "run.sh": "uv --directory ../permit-mcp/src/permit_mcp run server.py\n",
        },
    )

    found = messages(tmp_path)

    assert list(found) == [
        (".env.local", 2, "E1", SAFE),
        (".envrc", 1, "E1", REVIEW),
        (".github/workflows/deploy.yml", 4, "E1", REVIEW),
        ("Dockerfile", 2, "E1", REVIEW),
        ("compose.yaml", 5, "E1", SAFE),
        ("compose.yaml", 7, "E1", SAFE),
        ("deploy.sh", 1, "E1", REVIEW),
        ("run.sh", 1, "C1", REVIEW),
    ]
    assert found["Dockerfile", 2, "E1", REVIEW] == (
        "If this belongs to permit-mcp: PROJECT_ID is removed in 1.0; delete it (the server "
        "reads the project and environment from the API key)."
    )


def test_variable_uses_in_shell_are_not_assignments(tmp_path: Path) -> None:
    write(tmp_path, {"run.sh": 'echo $TENANT\necho "${TENANT:-default}" ${ENV_ID}\n'})

    assert findings(tmp_path) == []


@pytest.mark.parametrize(
    ("requirement", "expected"),
    [
        ("permit-mcp==0.1.0", ["D1"]),
        ("permit_mcp~=0.1", ["D1"]),
        ("permit-mcp>=0.1,<1", ["D1"]),
        ("-e ../permit-mcp", ["D1"]),
        ("permit-mcp @ file:///ABSOLUTE/PATH/TO/permit-mcp", ["D1"]),
        ("git+https://github.com/permitio/permit-mcp.git#egg=permit-mcp", ["D1"]),
        ("permit-mcp>=1.0,<2", []),
        ("permit-mcp", []),
        ("mcp==1.9.4", ["D2"]),
        ("mcp[cli]>=1.2,<2.0", ["D2"]),
        ("mcp<2", ["D2"]),
        ("mcp>=2.2.0,<3", []),
    ],
)
def test_requirements(tmp_path: Path, requirement: str, expected: list[str]) -> None:
    write(tmp_path, {"requirements.txt": f"httpx\n{requirement}\n"})

    assert [change for _, _, change, _ in findings(tmp_path)] == expected


def test_uv_source_that_points_at_a_clone(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "pyproject.toml": """\
                [project]
                dependencies = ["permit-mcp"]

                [tool.uv.sources]
                permit-mcp = { path = "../permit-mcp", editable = true }
            """
        },
    )

    assert findings(tmp_path) == [("pyproject.toml", 5, "D1", SAFE)]


@pytest.mark.parametrize(
    "directory",
    [".venv", "node_modules", "build", "dist", "app.egg-info", "env-with-pyvenv-cfg"],
)
def test_environments_and_build_output_are_skipped(tmp_path: Path, directory: str) -> None:
    old_code = "from permit_mcp import PermitServer\n"
    files = {f"{directory}/lib/host.py": old_code, "src/host.py": old_code}
    if directory == "env-with-pyvenv-cfg":
        files[f"{directory}/pyvenv.cfg"] = "home = /usr/bin\n"
    write(tmp_path, files)

    assert findings(tmp_path) == [("src/host.py", 1, "M1", REVIEW)]


def test_the_skill_directory_is_skipped() -> None:
    assert findings(SKILL_DIR.parent) == []


# ---------------------------------------------------------------------------
# The scanner script and SKILL.md
# ---------------------------------------------------------------------------


def scanner_ids() -> set[str]:
    """Return the finding IDs that the scanner's messages are filed under."""
    return set(re.findall(r'^    "([A-Z]\d)(?:-[a-z]+)?": ', SCANNER.read_text(), re.MULTILINE))


def test_the_sample_projects_cover_every_finding_id() -> None:
    covered = {change for _, _, change, _ in V0_1_FINDINGS} | {"E3"}

    assert covered == scanner_ids()


def test_skill_md_has_a_section_for_every_finding_id() -> None:
    headings = set(re.findall(r"^### ([A-Z]\d)\b", SKILL.read_text(), re.MULTILINE))

    assert headings == scanner_ids()


def test_skill_md_frontmatter() -> None:
    text = SKILL.read_text()
    match = re.match(r"---\nname: (?P<name>.+)\ndescription: (?P<description>.+)\n---\n", text)

    assert match, "SKILL.md must start with name and description frontmatter"
    assert match["name"] == SKILL_DIR.name
    for trigger in ("0.1", "1.0", "PermitServer", "fastmcp", "TENANT", "PERMIT_MCP_USER"):
        assert trigger in match["description"], trigger
    assert "user_id" in match["description"]


def test_skill_md_links_are_absolute() -> None:
    """The skill is installed outside this repository, so a relative link would not resolve."""
    targets = re.findall(r"\]\(([^)]+)\)", SKILL.read_text())

    assert targets
    for target in targets:
        assert target.startswith(("https://", "#")), target


def test_skill_md_links_to_this_repository_name_existing_files() -> None:
    prefix = "https://github.com/permitio/permit-mcp/blob/main/"
    for target in re.findall(r"\]\((" + re.escape(prefix) + r"[^)#]+)", SKILL.read_text()):
        assert (ROOT / target.removeprefix(prefix)).is_file(), target


def test_skill_md_runs_the_scanner_from_its_own_directory() -> None:
    assert "scripts/scan.py" in SKILL.read_text()
    assert SCANNER.stat().st_mode & 0o111, "scan.py must be executable; it has a shebang"


def test_scanner_uses_only_the_standard_library_and_python_3_10_syntax() -> None:
    tree = ast.parse(SCANNER.read_text(), feature_version=(3, 10))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert imported <= sys.stdlib_module_names | {"__future__"}


def python_blocks(markdown: str) -> list[str]:
    """Return the ```python blocks, leaving out those marked as 0.1 code."""
    pattern = r"(<!-- docs-check: skip, 0\.1 code -->\n)?```python\n(.*?)```"
    return [match[2] for match in re.finditer(pattern, markdown, re.DOTALL) if match[1] is None]


def test_skill_md_python_blocks_type_check(tmp_path: Path) -> None:
    blocks = python_blocks(SKILL.read_text())
    assert blocks, "SKILL.md has no 1.0 Python example to check"
    paths = []
    for number, block in enumerate(blocks, 1):
        path = tmp_path / f"block_{number}.py"
        path.write_text(block)
        paths.append(str(path))

    # The command is this interpreter's mypy, on files this test wrote.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "mypy", "--strict", "--cache-dir", str(tmp_path / ".cache"), *paths],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS * 3,
        check=False,
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stdout + result.stderr
