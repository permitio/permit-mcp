"""Tests for format_audit.py, the dependency audit's summary, annotations, Slack and gate.

Run with:
uv run --only-dev pytest -c .github/scripts/pytest.ini .github/scripts/test_format_audit.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).parent / "format_audit.py"

sys.path.insert(0, str(Path(__file__).parent))

from format_audit import (  # noqa: E402 - importable once sys.path has its directory
    NO_FIX,
    Finding,
    merge,
    parse_trivy,
    render,
    render_annotations,
    render_slack,
    trivy_scanned_nothing,
)


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False
    )


def finding(severity: str = "HIGH", fixed: str = "2.0", **kwargs: str) -> Finding:
    fields = {
        "vuln_id": "CVE-1",
        "package": "pkg",
        "installed": "1.0",
        "title": "t",
        "url": "",
        **kwargs,
    }
    return Finding(severity=severity, fixed=fixed, sources={"trivy"}, **fields)


def vuln(**overrides: str) -> dict[str, str]:
    return {
        "VulnerabilityID": "CVE-2026-0001",
        "PkgName": "aiohttp",
        "InstalledVersion": "3.14.3",
        "FixedVersion": "3.14.4",
        "Severity": "HIGH",
        "Title": "Request smuggling in the HTTP parser",
        "PrimaryURL": "https://avd.aquasec.com/nvd/cve-2026-0001",
        **overrides,
    }


def trivy_report(*vulns: dict[str, str]) -> dict[str, Any]:
    packages = [{"Name": vuln["PkgName"], "Version": vuln["InstalledVersion"]} for vuln in vulns]
    return {
        "SchemaVersion": 2,
        "Results": [
            {
                "Target": "requirements.txt",
                "Type": "pip",
                "Packages": packages or [{"Name": "aiohttp", "Version": "3.14.3"}],
                "Vulnerabilities": list(vulns),
            }
        ],
    }


def clean_report() -> dict[str, Any]:
    """What Trivy writes for a scanned file with no advisories: a Target, and packages."""
    return {
        "SchemaVersion": 2,
        "Results": [
            {
                "Target": "requirements.txt",
                "Class": "lang-pkgs",
                "Type": "pip",
                "Packages": [{"Name": "aiohttp", "Version": "3.14.3"}],
            }
        ],
    }


def write(tmp_path: Path, tree: str, content: object, pinned: int | None = None) -> str:
    """Plant a tree as audit-deps.sh writes it: its requirements and its Trivy report.

    The requirements pin as many packages as the report lists, unless `pinned` says.
    """
    report = tmp_path / f"trivy-{tree}.json"
    report.write_text(content if isinstance(content, str) else json.dumps(content))
    if pinned is None:
        results = content.get("Results") if isinstance(content, dict) else None
        pinned = sum(len(result.get("Packages") or []) for result in results or [])
    (tmp_path / tree).mkdir(exist_ok=True)
    pins = "".join(f"pkg{index}==1.0\n    # via permit-mcp\n" for index in range(pinned))
    (tmp_path / tree / "requirements.txt").write_text(f"# compiled\n{pins}")
    return tree


def audit(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run("--dir", str(tmp_path), *args)


# --- the gate ----------------------------------------------------------------


def test_gate_passes_a_clean_tree(tmp_path: Path) -> None:
    result = audit(tmp_path, write(tmp_path, "floor", clean_report()), "--gate")
    assert result.returncode == 0
    assert result.stdout == ""


def test_gate_blocks_a_fixable_high_finding(tmp_path: Path) -> None:
    result = audit(tmp_path, write(tmp_path, "floor", trivy_report(vuln())), "--gate")
    assert result.returncode == 1
    assert result.stdout == "", "--gate prints to stderr only"
    assert "HIGH CVE-2026-0001 aiohttp 3.14.3 -> 3.14.4" in result.stderr


def test_gate_blocks_a_fixable_critical_finding_in_any_tree(tmp_path: Path) -> None:
    clean = write(tmp_path, "ceiling", clean_report())
    vulnerable = write(tmp_path, "dev", trivy_report(vuln(Severity="CRITICAL")))
    assert audit(tmp_path, clean, vulnerable, "--gate").returncode == 1


@pytest.mark.parametrize("severity", ["MEDIUM", "LOW", "UNKNOWN"])
def test_gate_passes_findings_below_high(tmp_path: Path, severity: str) -> None:
    result = audit(
        tmp_path, write(tmp_path, "floor", trivy_report(vuln(Severity=severity))), "--gate"
    )
    assert result.returncode == 0


def test_gate_passes_a_high_finding_with_no_fix(tmp_path: Path) -> None:
    result = audit(
        tmp_path, write(tmp_path, "floor", trivy_report(vuln(FixedVersion=""))), "--gate"
    )
    assert result.returncode == 0


@pytest.mark.parametrize(
    "content",
    ["", "{{{ truncated", {"SchemaVersion": 2, "Results": None}, {"Results": []}],
    ids=["empty file", "not json", "results null", "no results"],
)
def test_gate_exits_2_when_a_scan_did_not_complete(tmp_path: Path, content: object) -> None:
    clean = write(tmp_path, "ceiling", clean_report())
    result = audit(tmp_path, clean, write(tmp_path, "floor", content), "--gate")
    assert result.returncode == 2, "a scan that did not complete must never pass"
    assert "trivy:floor" in result.stderr


def test_gate_exits_2_on_an_empty_tree(tmp_path: Path) -> None:
    # What Trivy writes, exiting 0, for a tree whose requirements.txt is empty.
    empty_tree = {"SchemaVersion": 2, "ArtifactName": "runtime-floor", "Results": None}
    result = audit(tmp_path, write(tmp_path, "floor", empty_tree), "--gate")
    assert result.returncode == 2
    assert "empty scan, not a clean one" in result.stderr


def test_gate_exits_2_on_a_missing_report(tmp_path: Path) -> None:
    result = audit(tmp_path, "absent", "--gate")
    assert result.returncode == 2
    assert "could not read" in result.stderr


def test_gate_exits_2_rather_than_1_when_a_tree_is_missing_beside_a_finding(
    tmp_path: Path,
) -> None:
    vulnerable = write(tmp_path, "ceiling", trivy_report(vuln()))
    result = audit(tmp_path, vulnerable, "absent", "--gate")
    assert result.returncode == 2


@pytest.mark.parametrize("pinned", [0, 2, 35])
def test_gate_exits_2_when_trivy_listed_other_than_the_tree(tmp_path: Path, pinned: int) -> None:
    # A scan without --list-all-pkgs lists no package, and a scan of the wrong
    # file lists other packages: neither covered the tree.
    result = audit(tmp_path, write(tmp_path, "floor", clean_report(), pinned=pinned), "--gate")
    assert result.returncode == 2
    assert "Trivy lists 1 packages but" in result.stderr
    assert f"pins {pinned}" in result.stderr


def test_gate_exits_2_when_a_report_lists_no_packages(tmp_path: Path) -> None:
    report = clean_report()
    del report["Results"][0]["Packages"]
    result = audit(tmp_path, write(tmp_path, "floor", report, pinned=1), "--gate")
    assert result.returncode == 2
    assert "without --list-all-pkgs" in result.stderr


def test_gate_exits_2_when_the_tree_is_missing(tmp_path: Path) -> None:
    write(tmp_path, "floor", clean_report())
    (tmp_path / "floor" / "requirements.txt").unlink()
    result = audit(tmp_path, "floor", "--gate")
    assert result.returncode == 2
    assert "could not read" in result.stderr


def test_no_tree_argument_exits_2(tmp_path: Path) -> None:
    assert audit(tmp_path, "--gate").returncode == 2


def test_no_dir_argument_exits_2() -> None:
    assert run("runtime-floor", "--gate").returncode == 2


# --- the summary -------------------------------------------------------------


def test_summary_of_a_clean_tree_says_clean(tmp_path: Path) -> None:
    result = audit(tmp_path, write(tmp_path, "floor", clean_report()), "--context", "the trees")
    assert result.returncode == 0
    assert "No known vulnerabilities" in result.stdout
    assert "_Scanned: the trees_" in result.stdout


@pytest.mark.parametrize("content", ["", "{{{", {"Results": None}])
def test_summary_of_a_broken_report_exits_0_and_does_not_say_clean(
    tmp_path: Path, content: object
) -> None:
    result = audit(tmp_path, write(tmp_path, "floor", content))
    assert result.returncode == 0, "the summary is still written; the gate fails the job"
    assert "The audit did not complete" in result.stdout
    assert "No known vulnerabilities" not in result.stdout


def test_one_broken_tree_does_not_hide_another_trees_findings(tmp_path: Path) -> None:
    vulnerable = write(tmp_path, "ceiling", trivy_report(vuln()))
    result = audit(tmp_path, vulnerable, write(tmp_path, "floor", "{{{"))
    assert "CVE-2026-0001" in result.stdout
    assert "The audit did not complete" in result.stdout


def test_summary_names_the_trees_a_finding_is_in(tmp_path: Path) -> None:
    ceiling = write(tmp_path, "runtime-ceiling", trivy_report(vuln()))
    floor = write(tmp_path, "runtime-floor", trivy_report(vuln()))
    result = audit(tmp_path, ceiling, floor)
    assert result.stdout.count("CVE-2026-0001") >= 1
    assert "| runtime-ceiling, runtime-floor |" in result.stdout


def test_summary_says_how_to_fix_a_blocking_finding() -> None:
    out = render([finding()], [], "")
    assert "1 fixable HIGH/CRITICAL advisory** block this change" in out
    assert "exclude-newer-package = { <package> = false }" in out


def test_an_unfixable_critical_is_not_described_as_none() -> None:
    out = render([finding("CRITICAL", NO_FIX)], [], "")
    assert "none HIGH or CRITICAL" not in out
    assert "1 HIGH/CRITICAL** with no fix yet" in out


def test_mixed_fixable_and_unfixable_counts_both() -> None:
    out = render([finding(), finding("CRITICAL", NO_FIX, vuln_id="CVE-2")], [], "")
    assert "1 fixable HIGH/CRITICAL advisory" in out
    assert "A further 1 HIGH/CRITICAL have no fix yet" in out


def test_findings_below_high_do_not_claim_to_block() -> None:
    assert "They do not block" in render([finding("MEDIUM")], [], "")


# --- untrusted advisory text -------------------------------------------------


def test_a_pipe_in_a_package_name_cannot_break_the_table() -> None:
    assert "evil\\|pkg" in render([finding(package="evil|pkg")], [], "")


def test_backticks_in_advisory_text_cannot_close_the_fence() -> None:
    out = render([finding(title="benign ``` <script>alert(1)</script>")], [], "")
    body = out.split("~~~~~~")[1]
    assert "```" in body


def test_a_non_https_url_is_not_a_link() -> None:
    out = render([finding(url="javascript:alert(1)")], [], "")
    assert "javascript:" not in out


def test_annotations_cannot_forge_a_workflow_command() -> None:
    out = render_annotations([finding("CRITICAL", title="one\n::error::forged\rmore")])
    assert "\n" not in out
    assert "\r" not in out
    assert out.startswith("::error title=CRITICAL: CVE-1 in pkg::")
    assert "%0A::error::forged%0Dmore" in out


def test_annotations_escape_percent_before_newlines() -> None:
    assert "100%25%0Anext" in render_annotations([finding(title="100%\nnext")])


def test_annotations_cover_blocking_findings_only() -> None:
    out = render_annotations(
        [
            finding("HIGH", vuln_id="CVE-HIGH"),
            finding("MEDIUM", vuln_id="CVE-MED"),
            finding("CRITICAL", NO_FIX, vuln_id="CVE-NOFIX"),
        ]
    )
    assert "CVE-HIGH" in out
    assert "CVE-MED" not in out
    assert "CVE-NOFIX" not in out


def test_slack_text_is_entity_escaped() -> None:
    out = render_slack(
        [finding(package="<!channel>&")], [], repo="o/r", run_url="", ci_result="success"
    )
    assert "`&lt;!channel&gt;&amp;`" in out


# --- the Slack message ---------------------------------------------------------


def test_slack_says_clean_and_carries_the_ci_result() -> None:
    out = render_slack([], [], repo="o/r", run_url="https://example.invalid/run", ci_result="ok")
    lines = out.split("\n")
    assert lines[0] == ":white_check_mark: *o/r: dependency audit clean*"
    assert lines[-2] == ">CI: ok"
    assert lines[-1] == "><https://example.invalid/run|View the run>"


@pytest.mark.parametrize(
    ("result", "line"),
    [
        ("success", ">e2e: success"),
        ("failure", ">e2e: failure (did not run, or a test failed)"),
        ("cancelled", ">e2e: cancelled (did not run to the end)"),
        ("skipped", ">e2e: skipped (did not run)"),
        ("<other>", ">e2e: &lt;other&gt; (did not run)"),
    ],
)
def test_slack_carries_the_e2e_result_after_ci_s(tmp_path: Path, result: str, line: str) -> None:
    out = render_slack([], [], repo="o/r", run_url="", ci_result="success", e2e_result=result)
    assert out.split("\n")[-3:] == [">CI: success", line, ">See the workflow run."]
    cli = audit(
        tmp_path,
        write(tmp_path, "floor", clean_report()),
        "--slack",
        "--ci-result",
        "success",
        "--e2e-result",
        result,
    )
    assert cli.returncode == 0, cli.stderr
    assert cli.stdout.split("\n")[-3] == line


def test_slack_leaves_out_the_e2e_line_without_a_result() -> None:
    out = render_slack([], [], repo="o/r", run_url="", ci_result="success")
    assert "e2e" not in out


@pytest.mark.parametrize(
    ("result", "line"),
    [
        ("success", ">e2e, latest PDP: success"),
        ("failure", ">e2e, latest PDP: failure (did not run, or a test failed)"),
        ("skipped", ">e2e, latest PDP: skipped (did not run)"),
    ],
)
def test_slack_carries_the_latest_pdp_leg_s_result_apart_from_e2e_s(
    tmp_path: Path, result: str, line: str
) -> None:
    out = render_slack(
        [],
        [],
        repo="o/r",
        run_url="",
        ci_result="success",
        e2e_result="success",
        e2e_pdp_latest_result=result,
    )
    assert out.split("\n")[-4:] == [">CI: success", ">e2e: success", line, ">See the workflow run."]
    cli = audit(
        tmp_path,
        write(tmp_path, "floor", clean_report()),
        "--slack",
        "--e2e-result",
        "success",
        "--e2e-pdp-latest-result",
        result,
    )
    assert cli.returncode == 0, cli.stderr
    assert cli.stdout.split("\n")[-4:-2] == [">e2e: success", line]


def test_slack_says_the_audit_did_not_complete(tmp_path: Path) -> None:
    result = audit(tmp_path, "absent", "--slack", "--repo", "o/r")
    assert result.returncode == 0
    assert "dependency audit did not complete" in result.stdout
    assert "clean" not in result.stdout.split("\n")[0]


def test_slack_lists_each_package_once_with_its_highest_fix(tmp_path: Path) -> None:
    report = trivy_report(
        vuln(),
        vuln(VulnerabilityID="CVE-2026-0002", FixedVersion="3.14.9", Severity="CRITICAL"),
    )
    result = audit(tmp_path, write(tmp_path, "floor", report), "--slack", "--ci-result", "failure")
    assert "*2 fixable HIGH/CRITICAL* advisories" in result.stdout
    assert ">• `aiohttp` 3.14.3: 2 advisories (CRITICAL worst), upgrade to `3.14.9`" in (
        result.stdout
    )
    assert ">CI: failure" in result.stdout


def test_slack_caps_the_package_list() -> None:
    findings = [finding(package=f"pkg{index:02}") for index in range(12)]
    out = render_slack(findings, [], repo="o/r", run_url="", ci_result="")
    assert ">…and 2 more packages." in out
    assert "pkg11" not in out


# --- parsing and merging -------------------------------------------------------


def test_parse_trivy_tolerates_malformed_nodes() -> None:
    assert parse_trivy(None) == []
    assert parse_trivy({"Results": None}) == []
    assert parse_trivy({"Results": ["not a dict"]}) == []
    assert parse_trivy({"Results": [{"Vulnerabilities": ["not a dict"]}]}) == []


def test_a_missing_fixed_version_reads_as_no_fix() -> None:
    assert parse_trivy(trivy_report(vuln(FixedVersion="")))[0].fixed == NO_FIX


def test_an_unknown_severity_is_kept_as_unknown() -> None:
    assert parse_trivy(trivy_report(vuln(Severity="SEVERE")))[0].severity == "UNKNOWN"


def test_merge_keeps_the_worst_severity_and_a_known_fix() -> None:
    low = finding("LOW", NO_FIX)
    low.sources = {"trivy:a"}
    high = finding("HIGH", "2")
    high.sources = {"trivy:b"}
    merged = merge([[low], [high]])
    assert len(merged) == 1
    assert (merged[0].severity, merged[0].fixed) == ("HIGH", "2")
    assert merged[0].sources == {"trivy:a", "trivy:b"}


def test_merge_sorts_critical_first() -> None:
    merged = merge([[finding("LOW", vuln_id="A"), finding("CRITICAL", vuln_id="B")]])
    assert [f.severity for f in merged] == ["CRITICAL", "LOW"]


@pytest.mark.parametrize(
    "doc", [None, {}, [], {"Results": None}, {"Results": []}, {"Results": [{"Class": "x"}]}]
)
def test_reports_with_no_scanned_target_are_detected(doc: object) -> None:
    assert trivy_scanned_nothing(doc) is True


def test_a_real_report_is_not_empty() -> None:
    assert trivy_scanned_nothing(clean_report()) is False
    assert trivy_scanned_nothing(trivy_report(vuln())) is False
