from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from pytest_httpserver import HTTPServer

from tests.support import SCOPE, SCOPE_PATH, base_url, settings_for

if TYPE_CHECKING:
    from collections.abc import Iterator

    from permit_mcp import Settings

ENV_VARIABLES = (
    "PERMIT_API_KEY",
    "PERMIT_RESOURCE",
    "PERMIT_TENANT",
    "PERMIT_API_URL",
    "PERMIT_PDP_URL",
    "PERMIT_ACCESS_REQUEST_ELEMENT",
    "PERMIT_OPERATION_APPROVAL_ELEMENT",
    "PERMIT_MCP_USER",
    "TENANT",
    "RESOURCE_KEY",
    "PROJECT_ID",
    "ENV_ID",
    "ACCESS_ELEMENTS_CONFIG_ID",
    "OPERATION_ELEMENTS_CONFIG_ID",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "NETRC",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own Permit, proxy and netrc variables out of every test."""
    for name in ENV_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def _serve() -> Iterator[HTTPServer]:
    server = HTTPServer(host="127.0.0.1", port=0, threaded=True)
    server.start()
    yield server
    server.clear()
    server.stop()


@pytest.fixture(scope="session")
def api_server() -> Iterator[HTTPServer]:
    """One local server for the session; `api` resets it for each test."""
    yield from _serve()


@pytest.fixture(scope="session")
def pdp_server() -> Iterator[HTTPServer]:
    """A second local server for the session, the PDP; `pdp` resets it for each test."""
    yield from _serve()


@pytest.fixture
def api(api_server: HTTPServer) -> HTTPServer:
    """Reset the mock Permit API so it only answers the API-key scope lookup."""
    api_server.clear()
    api_server.expect_request(SCOPE_PATH, method="GET").respond_with_json(SCOPE)
    return api_server


@pytest.fixture
def pdp(pdp_server: HTTPServer) -> HTTPServer:
    """Reset the mock PDP so it answers nothing."""
    pdp_server.clear()
    return pdp_server


@pytest.fixture
def settings(api: HTTPServer, pdp: HTTPServer) -> Settings:
    """Build Settings with both elements set, pointing at the mock API and the mock PDP."""
    return settings_for(base_url(api), pdp_url=base_url(pdp))


class RecordList(logging.Handler):
    """A handler that keeps every record it receives, fully formatted."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self.lines: list[str] = []
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.lines.append(self.format(record))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def permit_logs() -> Iterator[RecordList]:
    """Capture every record on the `permit_mcp` logger tree at DEBUG."""
    logger = logging.getLogger("permit_mcp")
    handler = RecordList()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield handler
    logger.removeHandler(handler)
    logger.setLevel(previous_level)
