"""Secrets never reach log output, even at DEBUG."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from permit_mcp._log import (
    RECENT_SECRETS,
    REDACTED,
    configure_cli_logging,
    get_logger,
    redact,
    redact_recent,
    scrub,
)
from tests.support import (
    API_KEY,
    CASES,
    ELEMENT_TOKEN,
    connected,
    error_text,
    payload,
    serve_case,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pytest_httpserver import HTTPServer

    from permit_mcp import Settings
    from tests.conftest import RecordList


async def test_tool_calls_never_log_secrets(
    api: HTTPServer,
    settings: Settings,
    permit_logs: RecordList,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    approve = CASES["approve_operation_approval"]
    create = CASES["create_access_request"]
    serve_case(api, approve)
    serve_case(api, create, status=500, response={"detail": f"{API_KEY} {ELEMENT_TOKEN}"})

    async with connected(settings) as client:
        ok = await client.call_tool("approve_operation_approval", approve.arguments)
        failed = await client.call_tool("create_access_request", create.arguments)

    assert payload(ok) == approve.expected
    assert "500" in error_text(failed)
    for text in (permit_logs.text, caplog.text):
        assert API_KEY not in text
        assert ELEMENT_TOKEN not in text


def test_secret_logged_directly_is_redacted(settings: Settings, permit_logs: RecordList) -> None:
    logger = logging.getLogger("permit_mcp")

    logger.warning("key in message %s", settings.api_key)
    logger.warning("key in args: %s", settings.api_key)
    logger.warning("key in message " + settings.api_key)  # noqa: G003 - exercising the filter

    assert len(permit_logs.records) == 3
    assert API_KEY not in permit_logs.text
    assert "[REDACTED]" in permit_logs.text


def test_traceback_is_redacted(permit_logs: RecordList) -> None:
    redact(API_KEY)
    logger = get_logger("tests.logging")

    try:
        message = f"rejected key {API_KEY}"
        raise RuntimeError(message)  # noqa: TRY301 - the traceback is what is under test
    except RuntimeError:
        logger.exception("call failed")

    assert "RuntimeError: rejected key [REDACTED]" in permit_logs.text
    assert API_KEY not in permit_logs.text


def test_malformed_format_is_redacted(permit_logs: RecordList) -> None:
    redact(API_KEY)

    get_logger("tests.logging").warning("value %d", API_KEY)

    assert len(permit_logs.records) == 1
    assert "value %d" in permit_logs.text
    assert REDACTED in permit_logs.text
    assert API_KEY not in permit_logs.text


def test_stack_info_is_redacted(permit_logs: RecordList) -> None:
    redact(API_KEY)
    logger = get_logger("tests.logging")
    record = logger.makeRecord(
        logger.name,
        logging.WARNING,
        __file__,
        1,
        "with a stack",
        (),
        None,
        sinfo=f"Stack (most recent call last):\n  token={API_KEY}",
    )

    logger.handle(record)

    assert f"token={REDACTED}" in permit_logs.text
    assert API_KEY not in permit_logs.text


@pytest.fixture
def root_logging() -> Iterator[None]:
    """Restore the root logger's handlers and level after a test configures it."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.mark.usefixtures("root_logging")
def test_cli_logging_redacts_the_records_of_other_libraries(
    capsys: pytest.CaptureFixture[str],
) -> None:
    redact(API_KEY)
    configure_cli_logging()

    logging.getLogger("mcp.server.lowlevel").warning("token %s", API_KEY)
    logging.getLogger("aiohttp.client").error("request failed for %s", API_KEY)
    print("protocol", file=sys.stdout)  # noqa: T201 - stdout must stay free of log records

    captured = capsys.readouterr()
    assert "WARNING mcp.server.lowlevel: token [REDACTED]" in captured.err
    assert "ERROR aiohttp.client: request failed for [REDACTED]" in captured.err
    assert API_KEY not in captured.err
    assert captured.out == "protocol\n"


def test_recent_secrets_are_bounded() -> None:
    redact(API_KEY)
    tokens = [f"recent-{uuid4().hex}" for _ in range(RECENT_SECRETS + 1)]

    for token in tokens:
        redact_recent(token)

    assert scrub(tokens[0]) == tokens[0]
    assert scrub(tokens[1]) == REDACTED
    assert scrub(tokens[-1]) == REDACTED
    assert scrub(API_KEY) == REDACTED


def test_recent_secret_that_is_also_permanent_stays_redacted() -> None:
    permanent = f"permanent-{uuid4().hex}"
    redact(permanent)
    redact_recent(permanent)

    for _ in range(RECENT_SECRETS):
        redact_recent(f"recent-{uuid4().hex}")

    assert scrub(permanent) == REDACTED
