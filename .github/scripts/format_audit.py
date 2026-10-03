#!/usr/bin/env python3
"""Render Trivy reports as a job summary, annotations or a Slack message, and gate on them.

Usage: format_audit.py --dir DIR TREE... [--gate | --annotations | --slack]

Reads the layout audit-deps.sh writes: for each TREE, DIR/TREE/requirements.txt
and its Trivy JSON report DIR/trivy-TREE.json. A report counts as complete only
when it lists as many packages as the tree pins. The modes:

* default: the markdown job summary on stdout. Exits 0 whatever the reports
  hold, so the summary is written even when a report is broken; it says so.
* --annotations: one `::error` workflow command per blocking advisory.
* --slack: the Slack message text, with the CI result when --ci-result is given,
  and the e2e suite's when --e2e-result is given.
* --gate: exits 0 when no fixable HIGH or CRITICAL advisory is present, 1 when
  one is, and 2 when a report is missing, unreadable, scanned nothing or
  scanned other than the tree's packages, so a scan that did not complete
  never passes.

An unparsable command line exits 2 (argparse). Stdlib only, so it runs on any
Python 3.11 or later with nothing installed.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

SEVERITY_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")
BLOCKING_SEVERITIES = frozenset({"CRITICAL", "HIGH"})

# Wherever Trivy reports no fixed version.
NO_FIX = "none available"

GATE_PASS = 0
GATE_BLOCKED = 1
GATE_INCOMPLETE = 2

# Six tildes rather than backticks: advisory text is third-party content, and a
# literal ``` in it would close a backtick fence and render the rest as markdown.
FENCE = "~~~~~~"

_ANNOTATION_MAX_CHARS = 200
_CELL_MAX_CHARS = 140
_DETAIL_MAX_CHARS = 1200
_SLACK_MAX_PACKAGES = 10

# What a failed or cancelled e2e job means, for the Slack message; any other result but
# success, such as skipped, reads "did not run".
E2E_NOT_RUN = {
    "failure": "did not run, or a test failed",
    "cancelled": "did not run to the end",
}


@dataclass(kw_only=True)
class Finding:
    """One advisory against one package, merged across the trees that report it."""

    vuln_id: str
    package: str
    installed: str
    severity: str
    fixed: str
    title: str
    url: str
    sources: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        """Map a severity Trivy may add later to UNKNOWN rather than drop the finding."""
        if self.severity not in SEVERITY_ORDER:
            self.severity = "UNKNOWN"

    @property
    def key(self) -> tuple[str, str]:
        """Identity used to merge the same advisory reported by several trees."""
        return (self.package, self.vuln_id)

    @property
    def blocking(self) -> bool:
        """HIGH or CRITICAL with a fix available.

        No version bump resolves an advisory without a fix, so blocking on it
        would hold every change until upstream moves. It is still reported.
        """
        return self.severity in BLOCKING_SEVERITIES and self.fixed != NO_FIX


def _one_line(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _md_cell(text: str) -> str:
    return _one_line(text, _CELL_MAX_CHARS).replace("|", "\\|").replace("`", "'")


def _load(path: str, label: str) -> tuple[object, str | None]:
    """Return (document, error). Never raises: a broken report is reported, not fatal."""
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, f"{label}: could not read {path}: {exc}"
    if not raw.strip():
        return None, f"{label}: {path} is empty"
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return None, f"{label}: {path} is not valid JSON: {exc}"


def trivy_scanned_nothing(doc: object) -> bool:
    """Whether a Trivy report holds no scanned package file.

    Trivy writes {"Results": null} and exits 0 when it recognises nothing to
    scan, such as an empty or misnamed requirements file. By findings alone
    that looks like a clean scan.
    """
    if not isinstance(doc, dict):
        return True
    results = doc.get("Results")
    if not isinstance(results, list) or not results:
        return True
    return not any(isinstance(result, dict) and result.get("Target") for result in results)


def parse_trivy(doc: object, source: str = "trivy") -> list[Finding]:
    """Extract the findings of a Trivy JSON report, skipping malformed entries."""
    findings: list[Finding] = []
    if not isinstance(doc, dict):
        return findings
    for result in doc.get("Results") or []:
        if not isinstance(result, dict):
            continue
        for vuln in result.get("Vulnerabilities") or []:
            if not isinstance(vuln, dict):
                continue
            findings.append(
                Finding(
                    vuln_id=str(vuln.get("VulnerabilityID") or "UNKNOWN"),
                    package=str(vuln.get("PkgName") or "unknown"),
                    installed=str(vuln.get("InstalledVersion") or "?"),
                    severity=str(vuln.get("Severity") or "UNKNOWN").upper(),
                    fixed=str(vuln.get("FixedVersion") or "") or NO_FIX,
                    title=str(vuln.get("Title") or vuln.get("Description") or ""),
                    url=str(vuln.get("PrimaryURL") or ""),
                    sources={source},
                )
            )
    return findings


def merge(groups: list[list[Finding]]) -> list[Finding]:
    """Merge the same advisory across trees, most severe first."""
    merged: dict[tuple[str, str], Finding] = {}
    for group in groups:
        for finding in group:
            existing = merged.get(finding.key)
            if existing is None:
                merged[finding.key] = finding
                continue
            existing.sources |= finding.sources
            if SEVERITY_ORDER.index(finding.severity) < SEVERITY_ORDER.index(existing.severity):
                existing.severity = finding.severity
            if existing.fixed == NO_FIX and finding.fixed != NO_FIX:
                existing.fixed = finding.fixed
    return sorted(
        merged.values(),
        key=lambda finding: (
            SEVERITY_ORDER.index(finding.severity),
            finding.package,
            finding.vuln_id,
        ),
    )


def _pinned_count(requirements: Path) -> int:
    lines = requirements.read_text(encoding="utf-8").splitlines()
    return sum(1 for line in lines if line[:1] not in {"", "#", " "} and "==" in line)


def _scanned_count(doc: object) -> int:
    if not isinstance(doc, dict):
        return 0
    return sum(
        len(result.get("Packages") or [])
        for result in doc.get("Results") or []
        if isinstance(result, dict) and result.get("Target")
    )


def load_tree(directory: Path, tree: str) -> tuple[list[Finding], str | None]:
    """Read one tree's Trivy report into findings, and the error that stops a pass."""
    label = f"trivy:{tree}"
    report = directory / f"trivy-{tree}.json"
    requirements = directory / tree / "requirements.txt"
    doc, error = _load(str(report), label)
    if error:
        return [], error
    findings = parse_trivy(doc, source=label)
    if trivy_scanned_nothing(doc):
        return findings, (
            f"{label}: the report holds no scanned package file. Trivy exits 0 when "
            "it recognises nothing to scan, so this is an empty scan, not a clean one."
        )
    try:
        pinned = _pinned_count(requirements)
    except OSError as exc:
        return findings, f"{label}: could not read {requirements}: {exc}"
    scanned = _scanned_count(doc)
    if scanned != pinned:
        return findings, (
            f"{label}: Trivy lists {scanned} packages but {requirements} pins {pinned}, "
            "so the scan did not cover the tree (was it run without --list-all-pkgs?)."
        )
    return findings, None


def load_reports(directory: Path, trees: list[str]) -> tuple[list[Finding], list[str]]:
    """Read every tree's report into merged findings and the errors that stop a pass."""
    errors: list[str] = []
    groups: list[list[Finding]] = []
    for tree in trees:
        findings, error = load_tree(directory, tree)
        groups.append(findings)
        if error:
            errors.append(error)
    return merge(groups), errors


def gate(findings: list[Finding], errors: list[str]) -> int:
    """Return the gate's exit status; a scan that did not complete outranks a finding."""
    if errors:
        return GATE_INCOMPLETE
    if any(finding.blocking for finding in findings):
        return GATE_BLOCKED
    return GATE_PASS


def _annotation_escape(text: str) -> str:
    """Escape a value for a workflow command, so advisory text cannot start a new one.

    % goes first, or it would corrupt the %0D and %0A added after it.
    """
    text = str(text)
    if len(text) > _ANNOTATION_MAX_CHARS:
        text = text[: _ANNOTATION_MAX_CHARS - 1] + "…"
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def render_annotations(findings: list[Finding]) -> str:
    """One `::error` workflow command per blocking finding."""
    lines = []
    for finding in findings:
        if not finding.blocking:
            continue
        title = _annotation_escape(f"{finding.severity}: {finding.vuln_id} in {finding.package}")
        body = _annotation_escape(
            f"{finding.package} {finding.installed}, fixed in {finding.fixed}. {finding.title}"
        )
        lines.append(f"::error title={title}::{body}")
    return "\n".join(lines)


def _slack_escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack_findings(findings: list[Finding], repo: str) -> list[str]:
    blockers = [finding for finding in findings if finding.blocking]
    severe = [finding for finding in findings if finding.severity in BLOCKING_SEVERITIES]
    if not findings:
        return [
            f":white_check_mark: *{_slack_escape(repo)}: dependency audit clean*",
            ">No known advisories in the newest or the lowest versions the ranges permit.",
        ]
    if blockers:
        icon, headline = ":rotating_light:", f"*{len(blockers)} fixable HIGH/CRITICAL* advisories"
    elif severe:
        icon, headline = ":rotating_light:", f"*{len(severe)} HIGH/CRITICAL* with no fix yet"
    else:
        icon, headline = ":large_orange_diamond:", f"{len(findings)} advisories, none HIGH/CRITICAL"
    lines = [f"{icon} *{_slack_escape(repo)}: dependency audit*", f">{headline}."]

    by_package: dict[str, list[Finding]] = {}
    for finding in blockers or severe or findings:
        by_package.setdefault(finding.package, []).append(finding)
    for package in sorted(by_package)[:_SLACK_MAX_PACKAGES]:
        group = by_package[package]
        worst = min(group, key=lambda finding: SEVERITY_ORDER.index(finding.severity))
        targets = sorted({finding.fixed for finding in group if finding.fixed != NO_FIX})
        target = f"upgrade to `{_slack_escape(targets[-1])}`" if targets else "no fix available"
        noun = "advisory" if len(group) == 1 else "advisories"
        lines.append(
            f">• `{_slack_escape(package)}` {_slack_escape(worst.installed)}: "
            f"{len(group)} {noun} ({worst.severity} worst), {target}"
        )
    if len(by_package) > _SLACK_MAX_PACKAGES:
        lines.append(f">…and {len(by_package) - _SLACK_MAX_PACKAGES} more packages.")
    return lines


def _e2e_line(result: str) -> str:
    """The e2e job's result; anything but success also says the suite did not run."""
    line = f">e2e: {_slack_escape(result)}"
    if result == "success":
        return line
    return f"{line} ({E2E_NOT_RUN.get(result, 'did not run')})"


def render_slack(  # noqa: PLR0913 - the message's parts, keyword-only past the errors
    findings: list[Finding],
    errors: list[str],
    *,
    repo: str,
    run_url: str,
    ci_result: str,
    e2e_result: str = "",
) -> str:
    """The Slack message: the findings themselves, not only a verdict."""
    if errors:
        lines = [
            f":warning: *{_slack_escape(repo)}: dependency audit did not complete*",
            ">A scan report is missing or unreadable, so a quiet report is not a clean tree.",
        ]
    else:
        lines = _slack_findings(findings, repo)
    if ci_result:
        lines.append(f">CI: {_slack_escape(ci_result)}")
    if e2e_result:
        lines.append(_e2e_line(e2e_result))
    lines.append(f"><{run_url}|View the run>" if run_url else ">See the workflow run.")
    return "\n".join(lines)


HOW_TO_FIX = (
    "Raise the affected lower bound in `pyproject.toml` (`[project].dependencies`, or the "
    "pin in the `dev` group) to at least the *Fixed in* version, then run `uv lock`. The "
    "published ranges are open, so the floor is what a consumer can install. If `uv lock` "
    "filters the fix out with `exclude-newer`, add `exclude-newer-package = { <package> = "
    "false }` under `[tool.uv]` until the release is 7 days old."
)


def _headline(findings: list[Finding]) -> list[str]:
    blockers = [finding for finding in findings if finding.blocking]
    severe = [finding for finding in findings if finding.severity in BLOCKING_SEVERITIES]
    if blockers:
        noun = "advisory" if len(blockers) == 1 else "advisories"
        lines = [f":x: **{len(blockers)} fixable HIGH/CRITICAL {noun}** block this change."]
        if len(severe) > len(blockers):
            lines.append(
                f"A further {len(severe) - len(blockers)} HIGH/CRITICAL have no fix yet "
                "and do not block."
            )
        return lines
    if severe:
        return [
            (
                f":warning: **{len(severe)} HIGH/CRITICAL** with no fix yet. They do not block, "
                "since no version bump resolves them, but they need a decision."
            )
        ]
    return [":warning: Advisories found, none HIGH or CRITICAL. They do not block."]


def _table_row(finding: Finding) -> str:
    advisory = _md_cell(finding.vuln_id)
    if finding.url.startswith("https://"):
        advisory = f"[{advisory}]({finding.url})"
    trees = ", ".join(sorted(source.partition(":")[2] or source for source in finding.sources))
    return (
        f"| {finding.severity} | `{_md_cell(finding.package)}` "
        f"| `{_md_cell(finding.installed)}` | `{_md_cell(finding.fixed)}` "
        f"| {advisory} | {_md_cell(trees)} |"
    )


def render(findings: list[Finding], errors: list[str], context: str) -> str:
    """The markdown job summary."""
    out = ["## Dependency audit", ""]
    if context:
        out += [f"_Scanned: {context}_", ""]
    if errors:
        out += [
            ":x: **The audit did not complete.** These reports are missing or unreadable:",
            "",
            FENCE,
            *errors,
            FENCE,
            "",
        ]
    if not findings:
        if not errors:
            out.append(":white_check_mark: **No known vulnerabilities.**")
        return "\n".join(out) + "\n"

    out += [*_headline(findings), ""]
    out += ["| Severity | Package | Installed | Fixed in | Advisory | Trees |"]
    out += ["| --- | --- | --- | --- | --- | --- |"]
    out += [_table_row(finding) for finding in findings]
    out += ["", "<details><summary>Advisory details</summary>", ""]
    for finding in findings:
        out += [f"**{finding.severity} {_md_cell(finding.vuln_id)}** (`{finding.package}`)", ""]
        if finding.title:
            out += [FENCE, _one_line(finding.title, _DETAIL_MAX_CHARS), FENCE, ""]
    out += ["</details>", "", "### How to fix", "", HOW_TO_FIX]
    return "\n".join(out) + "\n"


def main() -> int:
    """Parse the arguments, read the reports and print the requested output."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", required=True, type=Path, help="audit-deps.sh's output")
    parser.add_argument("trees", nargs="+", help="the trees to read, such as runtime-floor")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--gate", action="store_true", help="exit 0, 1 or 2; print nothing")
    mode.add_argument("--annotations", action="store_true", help="print ::error commands")
    mode.add_argument("--slack", action="store_true", help="print the Slack message text")
    parser.add_argument("--context", default="", help="what was scanned, for the summary")
    parser.add_argument("--repo", default="", help="repository name, for the Slack message")
    parser.add_argument("--run-url", default="", help="workflow run URL, for the Slack message")
    parser.add_argument("--ci-result", default="", help="the CI job's result, for Slack")
    parser.add_argument("--e2e-result", default="", help="the e2e job's result, for Slack")
    args = parser.parse_args()

    findings, errors = load_reports(args.dir, args.trees)
    for error in errors:
        print(error, file=sys.stderr)

    if args.gate:
        for finding in findings:
            if finding.blocking:
                print(
                    f"{finding.severity} {finding.vuln_id} {finding.package} "
                    f"{finding.installed} -> {finding.fixed}",
                    file=sys.stderr,
                )
        return gate(findings, errors)
    if args.annotations:
        rendered = render_annotations(findings)
        if rendered:
            print(rendered)
        return 0
    if args.slack:
        print(
            render_slack(
                findings,
                errors,
                repo=args.repo,
                run_url=args.run_url,
                ci_result=args.ci_result,
                e2e_result=args.e2e_result,
            )
        )
        return 0
    sys.stdout.write(render(findings, errors, args.context))
    return 0


if __name__ == "__main__":
    sys.exit(main())
