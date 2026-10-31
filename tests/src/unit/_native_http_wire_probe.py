"""Read actual response octets, including bodies an HTTP client would hide."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from functools import partial
from typing import Any, cast

from aiohttp import web
from aiohttp.test_utils import BaseTestServer, TestServer
from starlette.responses import PlainTextResponse

from ._native_http_probe import adapter


@asynccontextmanager
async def wire_server(
    asgi: Any,
    *,
    buffered: bool = False,
    prepare: Callable[[web.Request, web.StreamResponse], Awaitable[None]] | None = None,
) -> AsyncIterator[BaseTestServer]:
    module = adapter()

    async def handle(request: web.Request) -> web.StreamResponse:
        app = PlainTextResponse("next") if request.path == "/next" else asgi
        if buffered:
            return cast(
                web.Response,
                await module.serve_buffered_asgi(
                    app,
                    method=request.method,
                    path=request.path,
                    headers=[
                        (name.lower(), value) for name, value in request.raw_headers
                    ],
                    body=await request.read(),
                    state={},
                ),
            )
        return cast(web.StreamResponse, await module.serve_asgi(app, request))

    listener = web.Application()
    listener.router.add_route("*", "/{path:.*}", handle)
    if prepare is not None:
        listener.on_response_prepare.append(prepare)
    async with TestServer(listener) as server:
        yield server


async def request_then_get(server: BaseTestServer, method: str) -> tuple[bytes, bytes]:
    """The next response must start immediately after the bodyless headers."""
    reader, writer = await asyncio.open_connection(server.host, server.port)
    try:
        writer.write(f"{method} /mcp HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        await writer.drain()
        headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        writer.write(
            b"GET /next HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        following = await asyncio.wait_for(reader.read(), 5)
        return headers, following
    finally:
        writer.close()
        await writer.wait_closed()


def assert_next_response(following: bytes) -> None:
    assert following.startswith(b"HTTP/1.1 200 OK\r\n"), following
    assert following.partition(b"\r\n\r\n")[2] == b"next", following


async def no_body_on_wire(method: str, status: int) -> None:
    response = PlainTextResponse("abcdef", status_code=status, headers={"etag": '"v1"'})
    completed = asyncio.Event()

    async def asgi(scope: dict[str, Any], receive: Any, send: Any) -> None:
        # Split a genuine Starlette response into two chunks and an empty EOF.
        async def split_body(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.body":
                for chunk in (message["body"][:3], message["body"][3:]):
                    await send(
                        {"type": "http.response.body", "body": chunk, "more_body": True}
                    )
                await send({"type": "http.response.body"})
            else:
                await send(message)

        await response(scope, receive, split_body)
        assert await receive() == {"type": "http.disconnect"}
        completed.set()

    async with wire_server(asgi) as server:
        headers, following = await request_then_get(server, method)
    assert headers.startswith(f"HTTP/1.1 {status} ".encode()), headers
    assert b'etag: "v1"\r\n' in headers.lower(), headers
    assert b"transfer-encoding:" not in headers.lower(), headers
    if method == "HEAD":
        assert b"content-length: 6\r\n" in headers.lower(), headers
    else:
        assert b"content-length:" not in headers.lower(), headers
    assert completed.is_set(), "Suppressing the payload must still complete ASGI EOF"
    assert_next_response(following)


async def fastmcp_head_on_wire() -> None:
    from ha_mcp._vendor.fastmcp import FastMCP

    mcp = FastMCP("HEAD wire regression")
    app = mcp.http_app(path="/mcp", stateless_http=True, host_origin_protection=False)
    async with app.lifespan(app), wire_server(app) as server:
        headers, following = await request_then_get(server, "HEAD")
    assert headers.startswith(b"HTTP/1.1 405 Method Not Allowed\r\n"), headers
    assert b"content-length: 18\r\n" in headers.lower(), headers
    assert b"allow:" in headers.lower(), headers
    assert b"transfer-encoding:" not in headers.lower(), headers
    assert_next_response(following)


async def run(case: str) -> None:
    cases: dict[str, Callable[[], Awaitable[None]]] = {
        "head_wire": partial(no_body_on_wire, "HEAD", 200),
        "no_content_wire": partial(no_body_on_wire, "DELETE", 204),
        "not_modified_wire": partial(no_body_on_wire, "GET", 304),
        "fastmcp_head_wire": fastmcp_head_on_wire,
    }
    await cases[case]()
