"""The API reference site's pages: what scripts/docs_pages.py writes, and what mkdocs.yml lists.

The site itself is built by .github/scripts/build-docs.sh with the `docs` dependency group,
which this suite does not install.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from mcp.client import Client

import permit_mcp
from permit_mcp import TOOL_NAMES, Settings, create_server
from permit_mcp.config import DEFAULT_API_URL, DEFAULT_PDP_URL

if TYPE_CHECKING:
    from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "docs_pages.py"
DOCS = ROOT / "docs"
MKDOCS_YML = ROOT / "mkdocs.yml"
NAV_ENTRY = re.compile(r"^  - (?P<title>[^:]+): (?P<source>\S+\.md)$")
JSON_BLOCK = re.compile(r"^```json\n(.*?)^```$", re.MULTILINE | re.DOTALL)


@pytest.fixture(scope="module")
def docs_pages() -> ModuleType:
    spec = importlib.util.spec_from_file_location("docs_pages", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def listed_tools(**settings: str) -> list[dict[str, Any]]:
    server = create_server(
        Settings(
            api_key="test-key",
            access_request_element="access-element",
            operation_approval_element="approval-element",
            user="alice",
            **settings,
        )
    )
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
    return [tool.model_dump(by_alias=True, exclude_none=True, mode="json") for tool in tools]


def nav() -> list[tuple[str, str]]:
    """The (title, source) of each `nav` entry in mkdocs.yml, in order."""
    lines = MKDOCS_YML.read_text(encoding="utf-8").splitlines()
    start = lines.index("nav:") + 1
    entries: list[tuple[str, str]] = []
    for line in lines[start:]:
        match = NAV_ENTRY.match(line)
        if match is None:
            break
        entries.append((match["title"], match["source"]))
    return entries


def sections(page: str) -> dict[str, str]:
    """Split the Tools page into each tool's section, by its `## `name`` heading."""
    parts = re.split(r"^## `([a-z_]+)`$", page, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def test_the_script_writes_the_tools_page_and_llms_txt(tmp_path: Path) -> None:
    completed = subprocess.run(  # noqa: S603 - runs the script under test
        [sys.executable, str(SCRIPT), "pages", "--docs-dir", str(tmp_path / "docs")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    page = (tmp_path / "docs" / "reference" / "tools.md").read_text(encoding="utf-8")
    assert list(sections(page)) == list(TOOL_NAMES)
    assert (tmp_path / "docs" / "llms.txt").read_text(encoding="utf-8").startswith("# permit-mcp")


async def test_the_tools_page_shows_what_the_server_lists(docs_pages: ModuleType) -> None:
    tools = await listed_tools(resource="documents", tenant="default")
    page = docs_pages.tools_page(await docs_pages.list_tools())
    found = sections(page)
    assert list(found) == [tool["name"] for tool in tools]
    for tool in tools:
        section = found[tool["name"]]
        assert f"\n{tool['description']}\n" in section
        schemas = [json.loads(block) for block in JSON_BLOCK.findall(section)]
        expected = [tool["inputSchema"], tool["outputSchema"]]
        assert schemas == [docs_pages.without_titles(schema) for schema in expected]
        assert schemas[0]["properties"].keys() == tool["inputSchema"]["properties"].keys()
        assert '"title"' not in section, "pydantic's titles name private methods"
        for name, value in tool["annotations"].items():
            assert f"`{name}: {json.dumps(value)}`" in section


def test_a_tool_without_an_output_schema_or_annotations_has_no_such_section(
    docs_pages: ModuleType,
) -> None:
    tool = {"name": "plain", "description": "Plain.", "inputSchema": {"type": "object"}}
    section = sections(docs_pages.tools_page([tool]))["plain"]
    assert "Annotations: none." in section
    assert "### Input schema" in section
    assert "Output schema" not in section


def test_titles_are_dropped_but_a_property_named_title_is_kept(docs_pages: ModuleType) -> None:
    schema = {
        "title": "_create_thingArguments",
        "type": "object",
        "properties": {
            "title": {"title": "Title", "type": "string"},
            "tags": {"anyOf": [{"title": "Tag", "type": "string"}, {"type": "null"}]},
        },
        "$defs": {"title": {"title": "T", "type": "object"}},
    }
    assert docs_pages.without_titles(schema) == {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "tags": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "$defs": {"title": {"type": "object"}},
    }


def test_the_script_fails_when_the_server_lists_other_tools(
    docs_pages: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_tools() -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(docs_pages, "list_tools", no_tools)
    monkeypatch.setattr(sys, "argv", ["docs_pages.py", "pages", "--docs-dir", str(tmp_path)])
    with pytest.raises(SystemExit, match="not TOOL_NAMES"):
        docs_pages.main()
    assert not (tmp_path / "reference" / "tools.md").exists()


def test_the_settings_docstring_states_the_default_urls() -> None:
    doc = " ".join((Settings.__doc__ or "").split())
    assert f"`{DEFAULT_API_URL}` by default" in doc
    assert f"`{DEFAULT_PDP_URL}`, by default" in doc


def test_the_tools_page_says_which_settings_it_shows(docs_pages: ModuleType) -> None:
    page = docs_pages.tools_page([])
    assert "`PERMIT_RESOURCE=documents`" in page
    assert "the `default` tenant" in page


def test_llms_txt_links_every_page_in_nav_order(docs_pages: ModuleType) -> None:
    text = docs_pages.llms_txt()
    links = re.findall(r"^- \[([^]]+)\]\((\S+)\): ", text, flags=re.MULTILINE)
    assert links[: len(nav())] == [
        ("Overview", "https://permitio.github.io/permit-mcp/index.md"),
        ("Embedding API", "https://permitio.github.io/permit-mcp/reference/api/"),
        ("Tools", "https://permitio.github.io/permit-mcp/reference/tools/index.md"),
        ("Upgrading to 1.0", "https://permitio.github.io/permit-mcp/upgrade-to-1.0/index.md"),
    ]
    assert re.match(r"# permit-mcp\n\n> \S", text), "llms.txt opens with a title and a summary"


def test_mkdocs_nav_lists_the_pages_the_script_describes(docs_pages: ModuleType) -> None:
    assert nav() == [(title, source) for source, title, _ in docs_pages.PAGES]
    assert f"site_url: {docs_pages.SITE_URL}" in MKDOCS_YML.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("source", "url"),
    [
        ("index.md", "https://permitio.github.io/permit-mcp/"),
        ("reference/api.md", "https://permitio.github.io/permit-mcp/reference/api/"),
        ("upgrade-to-1.0.md", "https://permitio.github.io/permit-mcp/upgrade-to-1.0/"),
        ("reference/index.md", "https://permitio.github.io/permit-mcp/reference/"),
    ],
)
def test_page_urls_are_directory_urls(docs_pages: ModuleType, source: str, url: str) -> None:
    assert docs_pages.page_url(source) == url


def test_every_committed_page_exists() -> None:
    generated = {"reference/tools.md"}
    for _, source in nav():
        assert source in generated or (DOCS / source).is_file(), source


def test_the_api_page_documents_every_export() -> None:
    page = (DOCS / "reference" / "api.md").read_text(encoding="utf-8")
    documented = re.findall(r"^::: permit_mcp\.(\w+)$", page, flags=re.MULTILINE)
    exported = [name for name in permit_mcp.__all__ if name != "__version__"]
    assert sorted(documented) == sorted(exported)


# --- the Markdown copies beside the built pages ---------------------------------------------

SITE = "https://permitio.github.io/permit-mcp/"


@pytest.fixture(scope="module")
def copies(docs_pages: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """The copies `copies` writes into a site with every page built, by site path."""
    root = tmp_path_factory.mktemp("built")
    docs, site = root / "docs", root / "site"
    shutil.copytree(DOCS, docs, ignore=shutil.ignore_patterns("llms.txt", "tools.md"))
    run = [sys.executable, str(SCRIPT)]
    subprocess.run([*run, "pages", "--docs-dir", str(docs)], check=True)  # noqa: S603
    for _, source in nav():
        built = site / docs_pages.page_dir(source) / "index.html"
        built.parent.mkdir(parents=True, exist_ok=True)
        built.write_text("<html></html>", encoding="utf-8")
    subprocess.run(  # noqa: S603 - runs the script under test
        [*run, "copies", "--docs-dir", str(docs), "--site-dir", str(site)], check=True
    )
    return {
        path.relative_to(site).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(site.rglob("index.md"))
    }


def test_every_page_but_the_api_page_has_its_markdown_copy_beside_it(
    copies: dict[str, str],
) -> None:
    assert sorted(copies) == ["index.md", "reference/tools/index.md", "upgrade-to-1.0/index.md"]


def test_the_overview_s_copy_is_the_readme(copies: dict[str, str]) -> None:
    assert copies["index.md"] == (ROOT / "README.md").read_text(encoding="utf-8")


def test_the_upgrade_guide_s_copy_is_its_source(copies: dict[str, str]) -> None:
    source = (DOCS / "upgrade-to-1.0.md").read_text(encoding="utf-8")
    assert copies["upgrade-to-1.0/index.md"] == source
    assert "](#embedders)" in source, "an anchor on the same page is left alone"


def test_the_tools_page_s_copy_links_the_overview_s_copy(copies: dict[str, str]) -> None:
    page = copies["reference/tools/index.md"]
    assert list(sections(page)) == list(TOOL_NAMES)
    assert f"[README]({SITE}index.md#tools)" in page
    assert "../index.md" not in page


@pytest.mark.parametrize(
    ("source", "text", "linked"),
    [
        ("reference/tools.md", "[a](../index.md#x)", f"[a]({SITE}index.md#x)"),
        ("reference/tools.md", "[a](api.md#y)", f"[a]({SITE}reference/api/#y)"),
        ("index.md", "[a](reference/tools.md)", f"[a]({SITE}reference/tools/index.md)"),
        ("index.md", "[a](upgrade-to-1.0.md)", f"[a]({SITE}upgrade-to-1.0/index.md)"),
        ("index.md", "[a](#anchor)", "[a](#anchor)"),
        ("index.md", "[a](https://x.example/a.md)", "[a](https://x.example/a.md)"),
        ("index.md", "[a](/abs.md)", "[a](/abs.md)"),
        ("index.md", "[a](notes.txt)", "[a](notes.txt)"),
    ],
)
def test_a_link_to_another_page_points_at_its_copy(
    docs_pages: ModuleType, source: str, text: str, linked: str
) -> None:
    assert docs_pages.link_copies(text, source) == linked


def test_copies_fail_when_the_site_was_not_built(tmp_path: Path) -> None:
    completed = subprocess.run(  # noqa: S603 - runs the script under test
        [sys.executable, str(SCRIPT), "copies", "--site-dir", str(tmp_path / "site")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert "index.html was not built; build the site first." in completed.stderr
    assert not (tmp_path / "site").exists()
