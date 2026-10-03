"""`python -m permit_mcp` reports configuration problems cleanly."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from tests.support import API_KEY

TIMEOUT_SECONDS = 60
VALID_ENV = {
    "PERMIT_API_KEY": API_KEY,
    "PERMIT_RESOURCE": "documents",
    "PERMIT_ACCESS_REQUEST_ELEMENT": "ar-elem",
    # A local address only: a CLI that reached the network would fail here, not go online.
    "PERMIT_API_URL": "http://127.0.0.1:9",
}


def run_cli(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the CLI with only the given variables (plus PATH) and closed stdin."""
    return subprocess.run(
        [sys.executable, "-m", "permit_mcp"],
        env={"PATH": os.environ.get("PATH", ""), **env},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


@pytest.mark.parametrize("missing", ["PERMIT_API_KEY", "PERMIT_RESOURCE", "PERMIT_MCP_USER"])
def test_missing_configuration_exits_2_with_a_clear_message(missing: str) -> None:
    env = {**VALID_ENV, "PERMIT_MCP_USER": "alice"}
    del env[missing]

    completed = run_cli(env)

    assert completed.returncode == 2, completed.stderr
    assert missing in completed.stderr
    assert "Traceback" not in completed.stderr
    assert completed.stdout == ""
    assert API_KEY not in completed.stderr


def test_bad_url_exits_2_without_the_key() -> None:
    completed = run_cli({**VALID_ENV, "PERMIT_MCP_USER": "alice", "PERMIT_API_URL": "nope"})

    assert completed.returncode == 2, completed.stderr
    assert "Traceback" not in completed.stderr
    assert API_KEY not in completed.stderr
    assert completed.stdout == ""


@pytest.mark.parametrize(
    "url",
    [API_KEY, f"https://user:{API_KEY}@api.example.test", f"https://api.example.test?{API_KEY}"],
    ids=["key-as-url", "key-as-password", "key-in-query"],
)
def test_secret_in_the_url_is_not_printed(url: str) -> None:
    completed = run_cli({**VALID_ENV, "PERMIT_MCP_USER": "alice", "PERMIT_API_URL": url})

    assert completed.returncode == 2, completed.stderr
    assert "PERMIT_API_URL" in completed.stderr
    assert API_KEY not in completed.stderr
    assert API_KEY[:10] not in completed.stderr
