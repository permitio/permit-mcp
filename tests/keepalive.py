"""A minimal HTTP/1.1 keep-alive server that counts TCP connections.

The werkzeug server behind pytest-httpserver closes every connection after one response, so it
cannot show whether a client reuses connections. This one keeps each connection open and serves
JSON from a fixed route table.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType

READ_TIMEOUT = 10.0


class KeepAliveServer:
    """Serves `routes[(method, path)]` as JSON and records connections and requests."""

    def __init__(self, routes: Mapping[tuple[str, str], Any]) -> None:
        self.routes = dict(routes)
        self.connections = 0
        self.closed = 0
        self._all_closed = asyncio.Event()
        self.requests: list[tuple[str, str]] = []
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.sockets[0].getsockname()[:2]
        return f"http://{host}:{port}"

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._server is not None
        self._server.close()
        for writer in self._writers:
            writer.close()
        await asyncio.wait_for(self._server.wait_closed(), READ_TIMEOUT)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._all_closed.clear()
        self._writers.append(writer)
        # The client closing its pooled connection at shutdown is the normal way a connection ends.
        with contextlib.suppress(ConnectionError, asyncio.IncompleteReadError):
            while request := await self._read_request(reader):
                self.requests.append(request)
                found = request in self.routes
                body = json.dumps(self.routes.get(request, {"detail": "no route"})).encode()
                status = "200 OK" if found else "404 Not Found"
                head = (
                    f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n"
                )
                writer.write(head.encode() + body)
                await writer.drain()
        writer.close()
        self.closed += 1
        if self.closed == self.connections:
            self._all_closed.set()

    async def wait_until_all_closed(self) -> None:
        """Wait (bounded) until the client has closed every connection it opened."""
        await asyncio.wait_for(self._all_closed.wait(), READ_TIMEOUT)

    @staticmethod
    async def _read_request(reader: asyncio.StreamReader) -> tuple[str, str] | None:
        line = await asyncio.wait_for(reader.readline(), READ_TIMEOUT)
        if not line:
            return None
        method, target, _ = line.decode("latin-1").split(" ", 2)
        length = 0
        while True:
            header = await asyncio.wait_for(reader.readline(), READ_TIMEOUT)
            if header in {b"\r\n", b"\n", b""}:
                break
            name, _, value = header.decode("latin-1").partition(":")
            if name.strip().lower() == "content-length":
                length = int(value.strip())
        if length:
            await asyncio.wait_for(reader.readexactly(length), READ_TIMEOUT)
        return method, target.split("?", 1)[0]
