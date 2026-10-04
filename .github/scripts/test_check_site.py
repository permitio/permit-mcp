"""Tests for check_site.py, run on planted mkdocs.yml files, docs/ and site/ trees.

Run with:
uv run --only-dev pytest -c .github/scripts/pytest.ini .github/scripts/test_check_site.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "check_site.py"
REPO_ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(SCRIPT.parent))

from check_site import (  # noqa: E402 - importable once sys.path has it
    API_PAGE,
    built_page,
    read_config,
)

MKDOCS = """site_name: planted
site_url: https://example.github.io/planted/

nav:
  - Home: index.md
  - API: reference/api.md
  - Guide: guide.md

plugins: []
"""


def plant(root: Path, *, mkdocs: str = MKDOCS, pages: dict[str, str] | None = None) -> Path:
    """A built checkout: mkdocs.yml, docs/ with `pages`, and site/ with each nav page built.

    Each built page but the API page has its Markdown copy, index.md, beside it.
    """
    (root / "mkdocs.yml").write_text(mkdocs, encoding="utf-8")
    pages = pages or {"index.md": "# Home\n", "reference/api.md": "# API\n", "guide.md": "# G\n"}
    for source, text in pages.items():
        (root / "docs" / source).parent.mkdir(parents=True, exist_ok=True)
        (root / "docs" / source).write_text(text, encoding="utf-8")
    for built in ("index.html", "reference/api/index.html", "guide/index.html"):
        (root / "site" / built).parent.mkdir(parents=True, exist_ok=True)
        (root / "site" / built).write_text(
            "<a href='reference/api/'>API</a><a href='/planted/'>Home</a>"
            "<img src='../assets/logo.svg'><a href='https://docs.permit.io/'>guides</a>",
            encoding="utf-8",
        )
        if built != "reference/api/index.html":
            (root / "site" / built).with_name("index.md").write_text("# Page\n", encoding="utf-8")
    return root


def check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_complete_site_passes(tmp_path: Path) -> None:
    completed = check(plant(tmp_path))
    assert completed.returncode == 0, completed.stdout
    assert "every nav page, their Markdown copies, no orphan page" in completed.stdout


def test_a_nav_page_that_was_not_built_fails(tmp_path: Path) -> None:
    root = plant(tmp_path)
    (root / "site" / "reference" / "api" / "index.html").unlink()
    completed = check(root)
    assert completed.returncode == 1
    assert (
        "::error title=Docs site::nav lists reference/api.md, but the build wrote no "
        "site/reference/api/index.html"
    ) in completed.stdout


@pytest.mark.parametrize(
    ("copy", "source"), [("site/index.md", "index.md"), ("site/guide/index.md", "guide.md")]
)
def test_a_nav_page_without_its_markdown_copy_fails(tmp_path: Path, copy: str, source: str) -> None:
    root = plant(tmp_path)
    (root / copy).unlink()
    completed = check(root)
    assert completed.returncode == 1
    assert completed.stdout == (
        f"::error title=Docs site::nav lists {source}, but the build wrote no {copy}, its"
        " Markdown copy\n"
    )


def test_the_api_page_needs_no_markdown_copy(tmp_path: Path) -> None:
    root = plant(tmp_path)
    assert not (root / "site" / "reference" / "api" / "index.md").exists()
    assert check(root).returncode == 0
    assert API_PAGE == "reference/api.md"


@pytest.mark.parametrize("orphan", ["orphan.md", "reference/orphan.md"])
def test_a_page_left_out_of_nav_fails(tmp_path: Path, orphan: str) -> None:
    pages = {
        "index.md": "# Home\n",
        "reference/api.md": "# API\n",
        "guide.md": "# G\n",
        orphan: "# Orphan\n",
    }
    completed = check(plant(tmp_path, pages=pages))
    assert completed.returncode == 1
    assert f"docs/{orphan} is not in mkdocs.yml's nav" in completed.stdout


@pytest.mark.parametrize(
    "tag", ["<a href='/reference/tools/'>t</a>", "<img src='/logo.svg'>", "<a href='//cdn/x'>c</a>"]
)
def test_a_link_from_the_host_s_root_fails(tmp_path: Path, tag: str) -> None:
    root = plant(tmp_path)
    (root / "site" / "reference" / "api" / "index.html").write_text(tag, encoding="utf-8")
    completed = check(root)
    assert completed.returncode == 1
    assert "reference/api/index.html: /" in completed.stdout
    assert "starts with / outside the site's path /planted/" in completed.stdout


def test_a_missing_snippet_fails_even_when_its_page_was_built(tmp_path: Path) -> None:
    pages = {"index.md": '--8<-- "README.md"\n', "reference/api.md": "# API\n", "guide.md": ""}
    root = plant(tmp_path, pages=pages)
    assert check(root).returncode == 1
    assert "docs/index.md includes README.md, which is missing" in check(root).stdout
    (root / "README.md").write_text("# Read me\n", encoding="utf-8")
    assert check(root).returncode == 0


@pytest.mark.parametrize(
    "mkdocs",
    [
        "site_url: https://example.github.io/planted/\n",
        "site_url: https://example.github.io/planted/\nnav:\n  - Section:\n    - a.md\n",
        "nav:\n  - Home: index.md\n  - API: reference/api.md\n",
        "site_url: https://example.github.io/planted/\nnav:\nplugins: []\n",
    ],
    ids=["no nav", "nested nav", "no site_url", "empty nav"],
)
def test_a_config_it_cannot_read_exits_2(tmp_path: Path, mkdocs: str) -> None:
    completed = check(plant(tmp_path, mkdocs=mkdocs))
    assert completed.returncode == 2
    assert "::error title=Docs site::Cannot check the site:" in completed.stdout


@pytest.mark.parametrize(
    ("source", "built"),
    [
        ("index.md", "index.html"),
        ("upgrade-to-1.0.md", "upgrade-to-1.0/index.html"),
        ("reference/api.md", "reference/api/index.html"),
        ("reference/index.md", "reference/index.html"),
    ],
)
def test_pages_are_built_at_directory_urls(source: str, built: str) -> None:
    assert built_page(Path("site"), source) == Path("site") / built


def test_the_repository_s_mkdocs_yml_is_read() -> None:
    sources, site_path = read_config(REPO_ROOT / "mkdocs.yml")
    assert sources[0] == "index.md"
    assert "reference/tools.md" in sources
    assert API_PAGE in sources
    assert site_path == "/permit-mcp/"
