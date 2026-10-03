#!/usr/bin/env bash
# Builds the API reference site into site/ and fails on any warning or on a
# problem in what was built. Run from the repository root; the CI docs job and
# the Pages workflow both run it.
#
# Usage: build-docs.sh
#
# Writes the generated pages (scripts/docs_pages.py), which fails when the
# server lists other tools than TOOL_NAMES. Then builds with Zensical from a
# clean cache, since a cached build does not repeat the warnings of the pages
# it reuses; --clean also empties site/.
#
# `zensical build --strict` stops on a broken link or anchor and on an
# unresolved cross-reference. It prints Griffe's warnings about a docstring
# ("griffe: <file>:<line>: <message>") and still exits 0, so the log is read
# too: any line a clean build does not print fails, and that takes in every
# "Warning:" and "griffe:" line. Zensical ignores the nav and absolute-link
# settings of mkdocs.yml's `validation`, and drops a page whose snippet include
# is missing without a word, so check_site.py then checks site/ and docs/: every
# nav page was built, no page in docs/ is left out of nav, no link starts with
# "/" outside the site's path, and every snippet include exists. It runs after
# a failed build too, so a missing snippet is named whatever else failed.
#
# Exits 1 on a failed build, a warning or another unexpected line, or a problem
# check_site.py found, and 2 when the build did not finish or check_site.py
# could not read mkdocs.yml.
set -euo pipefail

clean_lines='Build started|No issues found|Build finished in [0-9.]+ ?[a-zµ]*s'
log=$(mktemp)
trap 'rm -f "$log"' EXIT

uv run --locked --group docs python scripts/docs_pages.py
status=0
uv run --locked --group docs zensical build --strict --clean >"$log" 2>&1 || status=$?
# Zensical colours its output even when it does not write to a terminal.
esc=$'\e'
sed -i.orig "s/${esc}\[[0-9;]*m//g" "$log"
rm -f "$log.orig"
cat "$log"

site_status=0
uv run --no-project --python 3.11 python .github/scripts/check_site.py || site_status=$?

if [[ $status -ne 0 ]]; then
  echo "::error title=Docs::zensical build exited ${status}; see the log above."
  exit 1
fi
# Every warning line is among these, as is anything else a clean build does not print.
unexpected=$(grep -vxE "$clean_lines" "$log" || true)
if [[ -n $unexpected ]]; then
  echo "::error title=Docs::The docs build printed warnings or other lines a clean build" \
    "does not print:"
  printf '%s\n' "$unexpected"
  exit 1
fi
if ! grep -qE '^Build finished in ' "$log"; then
  echo "::error title=Docs::The docs build did not finish."
  exit 2
fi
if [[ $site_status -ne 0 ]]; then
  exit "$site_status"
fi
echo "The docs built with no warning."
