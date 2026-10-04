"""The request recorder (tests/api_record.py) writes what the coverage report reads, and no more.

The recorder made here records requests of its own. When this run records too, the run's
recorder is paused around each test, so they never reach the run's record.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
import pytest

from tests.api_record import (
    CONTROL_PLANE,
    PDP,
    RECORD_ENV,
    RequestRecorder,
    active,
    origins_file,
)
from tests.support import (
    API_KEY,
    CASES,
    ELEMENT_TOKEN,
    PDP_PATH,
    REDIRECT_TOKEN,
    SCOPE_PATH,
    UNUSED_PDP_URL,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

ROOT = Path(__file__).resolve().parents[1]
WIRE_TESTS = (
    "tests/test_tools_wire.py::test_tool_sends_exact_request_and_returns_api_json",
    "tests/test_tools_wire.py::test_api_error_reports_status_without_the_api_key",
)
FIELDS = {"method", "origin", "path", "status", "test"}


@pytest.fixture(autouse=True)
def run_record_paused() -> Iterator[None]:
    """Keep this module's own requests out of the run's record, when the run records."""
    recorder = active()
    if recorder is None:
        yield
        return
    with recorder.paused():
        yield


def string_values(value: object) -> Iterator[str]:
    """Yield every string value nested in a JSON value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from string_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from string_values(item)


def record_wire_tests(record: Path) -> list[dict[str, Any]]:
    """Run the wire tests in a pytest of their own with the recorder on; return the record."""
    env = {**os.environ, RECORD_ENV: str(record)}
    completed = subprocess.run(  # noqa: S603 - this interpreter's pytest on this repository's tests
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *WIRE_TESTS],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]


def test_the_record_holds_method_origin_path_status_and_test_only(tmp_path: Path) -> None:
    record = tmp_path / "record.jsonl"
    lines = record_wire_tests(record)
    assert lines
    assert all(set(line) == FIELDS for line in lines)
    sent = {(line["method"], line["path"]) for line in lines}
    for case in CASES.values():
        assert {(call.method, call.path) for call in case.calls} <= sent
    assert ("GET", SCOPE_PATH) in sent
    assert {line["status"] for line in lines} == {200, 403}
    tests = {line["test"] for line in lines}
    assert all(test.startswith(WIRE_TESTS) for test in tests)
    assert len(tests) == 2 * len(CASES)

    origins = json.loads(origins_file(record).read_text(encoding="utf-8"))
    assert sorted(origins.values()) == [[CONTROL_PLANE], [PDP]]
    served = {origin: apis[0] for origin, apis in origins.items()}
    for line in lines:
        assert served[line["origin"]] == (PDP if line["path"] == PDP_PATH else CONTROL_PLANE)


def test_the_record_holds_no_secret_header_body_or_query(tmp_path: Path) -> None:
    record = tmp_path / "record.jsonl"
    record_wire_tests(record)
    text = record.read_text(encoding="utf-8")
    for secret in (API_KEY, ELEMENT_TOKEN, REDIRECT_TOKEN, "Bearer", "?"):
        assert secret not in text
    sent = {
        value
        for case in CASES.values()
        for call in case.calls
        for value in [*string_values(call.body), *call.query.values()]
    }
    leaked = {value for value in sent if f'"{value}"' in text}
    assert not leaked, f"request bodies or query values reached the record: {leaked}"


async def test_a_request_that_got_no_response_has_a_null_status(tmp_path: Path) -> None:
    record = tmp_path / "record.jsonl"
    recorder = RequestRecorder(record)
    try:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.ClientConnectionError):
                await session.post(f"{UNUSED_PDP_URL}/allowed?q=1", json={"key": API_KEY})
    finally:
        recorder.close()
    lines = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    assert lines == [
        {
            "method": "POST",
            "origin": UNUSED_PDP_URL,
            "path": "/allowed",
            "status": None,
            "test": None,
        }
    ]


async def test_a_paused_or_closed_recorder_records_nothing(tmp_path: Path) -> None:
    record = tmp_path / "record.jsonl"
    recorder = RequestRecorder(record)
    try:
        with recorder.paused():
            async with aiohttp.ClientSession() as session:
                with pytest.raises(aiohttp.ClientConnectionError):
                    await session.post(f"{UNUSED_PDP_URL}/allowed")
    finally:
        recorder.close()
    async with aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.ClientConnectionError):
            await session.post(f"{UNUSED_PDP_URL}/allowed")
    assert not record.read_text(encoding="utf-8")


def test_noted_origins_are_merged_into_the_origins_file(tmp_path: Path) -> None:
    record = tmp_path / "record.jsonl"
    recorder = RequestRecorder(record)
    try:
        recorder.note_origin("http://127.0.0.1:41001/v2", CONTROL_PLANE)
        recorder.note_origin("http://127.0.0.1:41001", PDP, CONTROL_PLANE)
        recorder.note_origin("https://permit.invalid", CONTROL_PLANE)
    finally:
        recorder.close()
    assert json.loads(origins_file(record).read_text(encoding="utf-8")) == {
        "http://127.0.0.1:41001": [CONTROL_PLANE, PDP],
        "https://permit.invalid": [CONTROL_PLANE],
    }
