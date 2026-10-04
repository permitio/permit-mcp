#!/usr/bin/env python3
"""Check that a pytest JUnit XML report ran every test it collected.

Usage: check_junit.py REPORT

A skipped test passes pytest, so a leg whose tests all skip, or that collects
none, would pass having tested nothing. Exits 0 when at least one test ran and
none was skipped, failed or errored; 1 when any test was skipped, failed or
errored; and 2 when the report is missing, unreadable or holds no test.
Stdlib only.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ALL_RAN = 0
NOT_ALL_RAN = 1
NO_RESULT = 2

_COUNTS = ("tests", "skipped", "failures", "errors")


def count(report: Path) -> dict[str, int]:
    """Sum the test counts of every <testsuite> in the report.

    Raises:
        OSError: The report cannot be read.
        ET.ParseError: The report is not XML.
        ValueError: A count attribute is not an integer.
    """
    root = ET.fromstring(report.read_text(encoding="utf-8"))  # noqa: S314 - pytest's own report
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    totals = dict.fromkeys(_COUNTS, 0)
    for suite in suites:
        for name in _COUNTS:
            totals[name] += int(suite.get(name, "0"))
    return totals


def main(argv: list[str]) -> int:
    """Check the report named in argv and return the exit status."""
    if len(argv) != 1:
        print("usage: check_junit.py REPORT", file=sys.stderr)
        return NO_RESULT
    report = Path(argv[0])
    try:
        totals = count(report)
    except (OSError, ET.ParseError, ValueError) as exc:
        print(f"::error title=Test report::Could not read {report}: {exc}")
        return NO_RESULT
    if totals["tests"] == 0:
        print(f"::error title=Test report::{report} holds no test; nothing ran.")
        return NO_RESULT
    not_run = {name: totals[name] for name in _COUNTS[1:] if totals[name]}
    if not_run:
        listed = ", ".join(f"{number} {name}" for name, number in not_run.items())
        print(f"::error title=Test report::Of {totals['tests']} tests, {listed}.")
        return NOT_ALL_RAN
    print(f"All {totals['tests']} tests ran and passed.")
    return ALL_RAN


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
