#!/usr/bin/env python3
"""Check the built API reference site for what Zensical does not report.

Usage: check_site.py [--root DIR]

Run after `zensical build`, from the repository root or with --root. Zensical
0.0.65 ignores the nav and absolute-link settings of mkdocs.yml's `validation`,
and drops a page whose snippet include is missing without a word. So this checks:

- every page in mkdocs.yml's `nav` was built into site/ (directory URLs), with
  its Markdown copy, index.md, beside its index.html, which llms.txt links;
- every Markdown file in docs/ is in `nav`, so none is built but unlisted;
- no href or src in site/ starts with "/" outside the site's own path, which
  a project site at https://<owner>.github.io/<repo>/ would send off the site;
- every snippet include (`--8<-- "file"`) in docs/ names a file that exists.

Exits 0 when all hold, 1 on a problem, each printed on its own line, and 2 when
mkdocs.yml has no `nav` or `site_url` this script can read. Stdlib only.
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

PASS = 0
PROBLEMS = 1
CANNOT_CHECK = 2

NAV_ENTRY = re.compile(r"^  - (?P<title>[^:]+): (?P<source>\S+\.md)$")
SITE_URL = re.compile(r"^site_url: (?P<url>\S+)$", re.MULTILINE)
SNIPPET = re.compile(r"""^\s*-+8<-+\s+(["'])(?P<path>[^"']+)\1\s*$""", re.MULTILINE)


class ConfigError(Exception):
    """mkdocs.yml has no nav or site_url in the form this script reads."""


def read_config(mkdocs_yml: Path) -> tuple[list[str], str]:
    """Return the sources of `nav`, in order, and the path of `site_url`.

    Only a flat nav of `  - Title: page.md` lines is read; anything else in the
    block is an error rather than a page silently left unchecked.
    """
    text = mkdocs_yml.read_text(encoding="utf-8")
    lines = text.splitlines()
    if "nav:" not in lines:
        msg = f"{mkdocs_yml} has no top-level `nav:`"
        raise ConfigError(msg)
    sources: list[str] = []
    for line in lines[lines.index("nav:") + 1 :]:
        if not line.startswith(" "):
            break
        match = NAV_ENTRY.match(line)
        if match is None:
            msg = f"cannot read the nav entry {line.strip()!r}; use `  - Title: page.md`"
            raise ConfigError(msg)
        sources.append(match["source"])
    if not sources:
        msg = f"{mkdocs_yml}'s nav lists no page"
        raise ConfigError(msg)
    site_url = SITE_URL.search(text)
    if site_url is None:
        msg = f"{mkdocs_yml} has no site_url"
        raise ConfigError(msg)
    path = urllib.parse.urlsplit(site_url["url"]).path or "/"
    return sources, path if path.endswith("/") else f"{path}/"


def built_page(site: Path, source: str) -> Path:
    """Return where a directory-URL build writes the page built from `source`."""
    stem = source.removesuffix(".md")
    if stem == "index" or stem.endswith("/index"):
        return site / stem.removesuffix("index") / "index.html"
    return site / stem / "index.html"


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del tag
        self.links += [value for name, value in attrs if name in {"href", "src"} and value]


def root_relative_links(site: Path, site_path: str) -> list[str]:
    """Return each `file: link` in site/'s HTML that starts with "/" outside `site_path`."""
    found: list[str] = []
    for page in sorted(site.rglob("*.html")):
        parser = _Links()
        parser.feed(page.read_text(encoding="utf-8"))
        found += [
            f"{page.relative_to(site)}: {link}"
            for link in parser.links
            if link.startswith("/") and not link.startswith(site_path)
        ]
    return found


def problems(root: Path) -> list[str]:
    """Return every problem found in root's docs/ and site/."""
    docs, site = root / "docs", root / "site"
    sources, site_path = read_config(root / "mkdocs.yml")
    found = [
        f"nav lists {source}, but the build wrote no {built_page(site, source).relative_to(root)}"
        for source in sources
        if not built_page(site, source).is_file()
    ]
    found += [
        f"nav lists {source}, but the build wrote no {copy.relative_to(root)}, its Markdown copy"
        for source in sources
        if not (copy := built_page(site, source).with_name("index.md")).is_file()
    ]
    found += [
        f"{path.relative_to(root)} is not in mkdocs.yml's nav"
        for path in sorted(docs.rglob("*.md"))
        if path.relative_to(docs).as_posix() not in sources
    ]
    found += [
        f"{link} starts with / outside the site's path {site_path}"
        for link in root_relative_links(site, site_path)
    ]
    for path in sorted(docs.rglob("*.md")):
        for match in SNIPPET.finditer(path.read_text(encoding="utf-8")):
            if not (root / match["path"]).is_file():
                found.append(f"{path.relative_to(root)} includes {match['path']}, which is missing")
    return found


def main() -> int:
    """Check the site, print each problem as an annotation, and return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path())
    root: Path = parser.parse_args().root
    try:
        found = problems(root)
    except (ConfigError, OSError) as exc:
        print(f"::error title=Docs site::Cannot check the site: {exc}")
        return CANNOT_CHECK
    for problem in found:
        print(f"::error title=Docs site::{problem}")
    if found:
        return PROBLEMS
    print(
        "The site has every nav page and its Markdown copy, no orphan page, no root-relative"
        " link, every snippet."
    )
    return PASS


if __name__ == "__main__":
    sys.exit(main())
