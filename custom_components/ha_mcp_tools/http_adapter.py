"""Run HTTP ASGI invocations on HA's aiohttp listener with real backpressure."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web
from aiohttp.helpers import must_be_empty_body
from multidict import CIMultiDict

ASGIMessage = dict[str, Any]
ASGIApp = Callable[
    [
        dict[str, Any],
        Callable[[], Awaitable[ASGIMessage]],
        Callable[[ASGIMessage], Awaitable[None]],
    ],
    Awaitable[None],
]

_HEADER_NAME = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_HEADER_CONTROL = re.compile(rb"[\x00-\x08\x0a-\x1f\x7f]")


async def _read_body(request: web.Request, maximum: int) -> bytes:
    if maximum < 0:
        raise ValueError("max_body_bytes must be non-negative")

    def too_large(size: int) -> web.HTTPRequestEntityTooLarge:
        error = web.HTTPRequestEntityTooLarge(max_size=maximum, actual_size=size)
        error.force_close()
        return error

    if request.content_length is not None and request.content_length > maximum:
        raise too_large(request.content_length)
    body = bytearray()
    while chunk := await request.content.read(min(65_536, maximum - len(body) + 1)):
        if len(body) + len(chunk) > maximum:
            raise too_large(len(body) + len(chunk))
        body.extend(chunk)
    return bytes(body)


def _scope(
    request: web.Request, state: dict[str, Any], path: str | None
) -> dict[str, Any]:
    peer = request.get_extra_info("peername")
    local = request.get_extra_info("sockname")
    client_port = (
        peer[1]
        if isinstance(peer, tuple) and len(peer) >= 2 and peer[0] == request.remote
        else 0
    )
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": f"{request.version.major}.{request.version.minor}",
        "method": request.method,
        "scheme": request.scheme,
        "path": path or request.path,
        "raw_path": path.encode()
        if path
        else request.rel_url.raw_path.encode("ascii", "surrogateescape"),
        "query_string": request.rel_url.raw_query_string.encode(
            "ascii", "surrogateescape"
        ),
        "root_path": "",
        "headers": [(key.lower(), value) for key, value in request.raw_headers],
        "client": (request.remote, client_port) if request.remote is not None else None,
        "server": local[:2] if isinstance(local, tuple) else None,
        "state": state,
    }


def _headers(message: ASGIMessage) -> CIMultiDict[str]:
    """Accept ASCII token names and UTF-8 values, including plain ASCII.

    aiohttp serializes response headers as UTF-8. This is an explicit subset
    of ASGI's opaque-byte contract: unsupported bytes or forbidden controls
    fail with a generic 502 before prepare, rather than changing on the wire.
    """
    if message.get("trailers"):
        raise NotImplementedError("ASGI response trailers are not supported")
    headers: CIMultiDict[str] = CIMultiDict()
    for key, value in message.get("headers", []):
        if _HEADER_NAME.fullmatch(key) is None or _HEADER_CONTROL.search(value):
            raise web.HTTPBadGateway(text="Unsupported ASGI response header")
        try:
            decoded = value.decode("utf-8")
        except UnicodeDecodeError:
            raise web.HTTPBadGateway(text="Unsupported ASGI response header") from None
        if key.lower() != b"transfer-encoding":
            headers.add(key.decode("ascii"), decoded)
    if "text/event-stream" in headers.get("Content-Type", ""):
        headers.update(
            {
                "Cache-Control": "no-cache, no-transform",
                "Content-Encoding": "identity",
                "X-Accel-Buffering": "no",
            }
        )
    return headers


class _Invocation:
    """State belongs to this HTTP request; the adapter owns no helper tasks."""

    def __init__(self, request: web.Request, body: bytes) -> None:
        self.request = request
        self.body = body
        self.delivered = False
        self.complete = False
        self.closed = asyncio.Event()
        self.response: web.StreamResponse | None = None
        self.suppress_body = False

    def disconnected(self) -> bool:
        transport = self.request.transport
        return self.closed.is_set() or transport is None or transport.is_closing()

    async def receive(self) -> ASGIMessage:
        if self.disconnected():
            self.closed.set()
            return {"type": "http.disconnect"}
        if not self.delivered:
            self.delivered = True
            return {"type": "http.request", "body": self.body, "more_body": False}
        # Body EOF is not a disconnect. HA cancels the handler on peer loss;
        # response completion explicitly wakes this wait.
        await self.closed.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: ASGIMessage) -> None:
        empty_eof = (
            message["type"] == "http.response.body"
            and not message.get("body")
            and not message.get("more_body", False)
            and self.response is not None
            and not self.complete
        )
        if self.disconnected():
            self.closed.set()
            if empty_eof:
                # MCP clients may close as soon as the final SSE result arrives.
                # No application bytes remain; a lost payload must still fail.
                self.complete = True
                return
            raise ConnectionResetError("ASGI response connection is closed")
        try:
            if message["type"] == "http.response.start":
                await self.start(message)
            elif message["type"] == "http.response.body":
                await self.write(message)
            else:
                raise RuntimeError(f"Unsupported ASGI message: {message['type']}")
        except OSError:
            self.closed.set()
            if empty_eof:
                self.complete = True
                return
            raise

    async def start(self, message: ASGIMessage) -> None:
        if self.response is not None:
            raise RuntimeError("ASGI response already started")
        self.response = web.StreamResponse(
            status=message["status"], headers=_headers(message)
        )
        self.suppress_body = must_be_empty_body(
            self.request.method, self.response.status
        )
        await self.response.prepare(self.request)

    async def write(self, message: ASGIMessage) -> None:
        if self.response is None:
            raise RuntimeError("ASGI response body precedes response start")
        # StreamResponse strips forbidden framing, but write() still sends bytes.
        if (payload := message.get("body", b"")) and not self.suppress_body:
            await self.response.write(payload)
        if not message.get("more_body", False):
            await self.response.write_eof()
            self.complete = True
            self.closed.set()

    def finish(self, success: bool) -> None:
        self.closed.set()
        if self.response is not None and not success:
            # Never convert a partial stream into a misleading successful EOF.
            self.response.force_close()
            if (transport := self.request.transport) is not None:
                transport.close()


async def serve_asgi(
    app: ASGIApp,
    request: web.Request,
    *,
    lifespan_state: dict[str, Any] | None = None,
    request_state: dict[str, Any] | None = None,
    max_body_bytes: int = 1_048_576,
    path: str | None = None,
) -> web.StreamResponse:
    """Call the full app; trusted state and optional route mapping come from HA."""
    state = {**(lifespan_state or {}), **(request_state or {})}
    invocation = _Invocation(request, await _read_body(request, max_body_bytes))
    success = False
    try:
        await app(_scope(request, state, path), invocation.receive, invocation.send)
        if invocation.response is None or not invocation.complete:
            raise RuntimeError("ASGI application returned without a complete response")
        success = True
        return invocation.response
    finally:
        invocation.finish(success)


class _BufferedInvocation:
    """A cloudhook has no socket and requires a bounded, complete response."""

    def __init__(self, body: bytes, maximum: int) -> None:
        self.body = body
        self.maximum = maximum
        self.delivered = False
        self.finished = asyncio.Event()
        self.chunks = bytearray()
        self.headers: CIMultiDict[str] = CIMultiDict()
        self.status: int | None = None

    async def receive(self) -> ASGIMessage:
        if not self.delivered:
            self.delivered = True
            return {"type": "http.request", "body": self.body, "more_body": False}
        await self.finished.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: ASGIMessage) -> None:
        if message["type"] == "http.response.start":
            if self.status is not None:
                raise RuntimeError("ASGI response already started")
            self.status = message["status"]
            self.headers = _headers(message)
        elif message["type"] == "http.response.body":
            if self.status is None:
                raise RuntimeError("ASGI body precedes response start")
            payload = message.get("body", b"")
            if len(self.chunks) + len(payload) > self.maximum:
                raise web.HTTPBadGateway(text="Cloudhook response exceeds buffer limit")
            self.chunks.extend(payload)
            if not message.get("more_body", False):
                self.finished.set()
        else:
            raise RuntimeError("Unsupported buffered ASGI message")


async def serve_buffered_asgi(
    app: ASGIApp,
    *,
    method: str,
    path: str,
    headers: list[tuple[bytes, bytes]],
    body: bytes,
    state: dict[str, Any],
    max_response_bytes: int = 4_194_304,
) -> web.Response:
    """Buffer cloudhook replies only; the caller owns their time budget."""
    invocation = _BufferedInvocation(body, max_response_bytes)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "state": dict(state),
        "client": None,
        "server": None,
    }
    try:
        await app(scope, invocation.receive, invocation.send)
        if invocation.status is None or not invocation.finished.is_set():
            raise RuntimeError("ASGI application did not finish its response")
        return web.Response(
            status=invocation.status,
            headers=invocation.headers,
            body=bytes(invocation.chunks),
        )
    finally:
        invocation.finished.set()
