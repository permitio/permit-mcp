"""Records the HTTP requests the server sends during the offline tests.

The API coverage report (.github/scripts/api_coverage.py) reads the record to learn which
Permit API and PDP operations the tests exercise. tests/conftest.py calls `start` from
its pytest_configure, which does nothing unless PERMIT_MCP_API_RECORD names a file.

When it is set, every aiohttp.ClientSession created during the run gets a trace config,
and each request it sends becomes one JSON line: the HTTP method, the origin
(scheme://host:port), the URL path as sent (still percent-encoded, without the query
string), the response status (null when no response arrived) and the node id of the test
that sent it. Nothing else is written: no query string, header or body, so no API key,
token or request content.

Beside the record, `<record stem>.origins.json` maps each origin a test server listens on
to the APIs it serves ("control-plane", "pdp"), as the tests declare with
`note_origin`. The report matches a request only against the APIs of its origin.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

import aiohttp
import pytest
from yarl import URL

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator
    from types import SimpleNamespace

RECORD_ENV = "PERMIT_MCP_API_RECORD"
PLUGIN_NAME = "permit-mcp-api-record"
CONTROL_PLANE = "control-plane"
PDP = "pdp"

_active: RequestRecorder | None = None


def start(config: pytest.Config) -> None:
    """Start recording when PERMIT_MCP_API_RECORD names a file; otherwise do nothing."""
    global _active  # noqa: PLW0603 - one recorder per run, which note_origin reaches
    target = os.environ.get(RECORD_ENV)
    if not target:
        return
    _active = RequestRecorder(Path(target))
    config.pluginmanager.register(_active, PLUGIN_NAME)
    config.add_cleanup(_active.close)


def active() -> RequestRecorder | None:
    """Return the recorder of this run, or None when the run records nothing."""
    return _active


def note_origin(url: str, *apis: str) -> None:
    """Declare that the server at `url` serves `apis`, when this run records requests."""
    if _active is not None:
        _active.note_origin(url, *apis)


def origins_file(record: Path) -> Path:
    """Return the file beside `record` that maps origins to the APIs they serve."""
    return record.with_name(f"{record.stem}.origins.json")


def origin_of(url: URL) -> str:
    """Return `url`'s scheme, host and port, as the record and the origins file write it."""
    return str(url.origin())


class RequestRecorder:
    """Writes one line per request, attributed to the test that sent it."""

    def __init__(self, path: Path) -> None:
        """Open the record, truncating it, and trace every session created from now on.

        Args:
            path: The record file; its directory is created if missing.

        """
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file: IO[str] | None = path.open("w", encoding="utf-8")
        self._origins_path = origins_file(path)
        self._origins: dict[str, list[str]] = {}
        self._origins_path.write_text("{}\n", encoding="utf-8")
        self._lock = threading.Lock()
        self._test: str | None = None
        self._paused = False

        trace = aiohttp.TraceConfig()
        trace.on_request_end.append(self._on_request_end)
        trace.on_request_exception.append(self._on_request_exception)
        original_init = aiohttp.ClientSession.__init__

        def traced_init(
            session: aiohttp.ClientSession,
            *args: Any,  # noqa: ANN401 - forwarded to ClientSession unchanged
            **kwargs: Any,  # noqa: ANN401 - forwarded to ClientSession unchanged
        ) -> None:
            configs = list(kwargs.pop("trace_configs", None) or [])
            original_init(session, *args, trace_configs=[*configs, trace], **kwargs)

        self._patch = pytest.MonkeyPatch()
        self._patch.setattr(aiohttp.ClientSession, "__init__", traced_init)

    def close(self) -> None:
        """Stop tracing new sessions and close the record."""
        self._patch.undo()
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None

    def note_origin(self, url: str, *apis: str) -> None:
        """Add `apis` to those the origin of `url` serves, and rewrite the origins file."""
        with self._lock:
            served = self._origins.setdefault(origin_of(URL(url)), [])
            served.extend(api for api in apis if api not in served)
            text = json.dumps(self._origins, indent=2, sort_keys=True) + "\n"
            self._origins_path.write_text(text, encoding="utf-8")

    @contextmanager
    def paused(self) -> Iterator[None]:
        """Record nothing inside the block, for tests whose requests are not the server's."""
        self._paused = True
        try:
            yield
        finally:
            self._paused = False

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item: pytest.Item) -> Generator[None, object, object]:
        """Attribute the requests of a test's setup, call and teardown to that test."""
        self._test = item.nodeid
        try:
            return (yield)
        finally:
            self._test = None

    async def _on_request_end(
        self,
        _session: aiohttp.ClientSession,
        _context: SimpleNamespace,
        params: aiohttp.TraceRequestEndParams,
    ) -> None:
        self._write(params.method, params.url, params.response.status)

    async def _on_request_exception(
        self,
        _session: aiohttp.ClientSession,
        _context: SimpleNamespace,
        params: aiohttp.TraceRequestExceptionParams,
    ) -> None:
        self._write(params.method, params.url, None)

    def _write(self, method: str, url: URL, status: int | None) -> None:
        line = {
            "method": method.upper(),
            "origin": origin_of(url),
            "path": url.raw_path,
            "status": status,
            "test": self._test,
        }
        with self._lock:
            if self._file is not None and not self._paused:
                self._file.write(json.dumps(line, sort_keys=True) + "\n")
                self._file.flush()
