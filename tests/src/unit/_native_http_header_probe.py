"""Wire contracts for the adapter's explicit UTF-8 response-header subset."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from aiohttp import web
from aiohttp.test_utils import BaseTestServer
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse

from ._native_http_wire_probe import wire_server


async def raw_request(
    server: BaseTestServer, extra: bytes = b"", method: str = "GET", body: bytes = b""
) -> bytes:
    reader, writer = await asyncio.open_connection(server.host, server.port)
    try:
        writer.write(
            f"{method} / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
            f"Content-Length: {len(body)}\r\n".encode("ascii")
            + extra
            + b"\r\n"
            + body
        )
        await writer.drain()
        return await asyncio.wait_for(reader.read(), 5)
    finally:
        writer.close()
        await writer.wait_closed()


def header_values(raw: bytes, name: bytes) -> list[bytes]:
    return [
        line.partition(b": ")[2]
        for line in raw.partition(b"\r\n\r\n")[0].split(b"\r\n")[1:]
        if line.partition(b":")[0].lower() == name
    ]


async def conditional_headers(buffered: bool) -> None:
    """ASCII/UTF-8 ETags must round-trip; duplicates retain bytes and order."""
    for etag in (b'"ascii"', b'"caf\xc3\xa9"'):
        repeated = [b"first\tvalue", etag, b"snowman-\xe2\x98\x83"]

        async def app(
            scope: dict[str, Any],
            receive: Any,
            send: Any,
            *,
            etag: bytes = etag,
            repeated: list[bytes] = repeated,
        ) -> None:
            supplied = next(
                (v for k, v in scope["headers"] if k == b"if-none-match"), None
            )
            response = PlainTextResponse(
                "" if supplied == etag else "ok",
                status_code=304 if supplied == etag else 200,
            )
            response.raw_headers += [(b"etag", etag)]
            response.raw_headers.extend((b"x-repeat", value) for value in repeated)
            await response(scope, receive, send)

        async with wire_server(app, buffered=buffered) as server:
            first = await raw_request(server)
            emitted = header_values(first, b"etag")
            assert emitted == [etag], first
            assert header_values(first, b"x-repeat") == repeated, first
            second = await raw_request(
                server, b"If-None-Match: " + emitted[0] + b"\r\n"
            )
        assert second.startswith(b"HTTP/1.1 304 "), second
        assert second.partition(b"\r\n\r\n")[2] == b"", second


async def rejected_headers(buffered: bool) -> None:
    """Reject unsupported bytes/names/controls before any ASGI header is sent."""
    prepared: list[int] = []

    async def prepare(request: web.Request, response: web.StreamResponse) -> None:
        prepared.append(response.status)

    for name, value in (
        (b"x-secret", b"sensitive-\xe9"),
        (b"secret-\xe9", b"sensitive"),
        (b"secret:name", b"sensitive"),
        (b"secret name", b"sensitive"),
        (b"", b"sensitive"),
        (b"x-secret", b"sensitive\r\nInjected: yes"),
        (b"x-secret", b"sensitive\nInjected: yes"),
        (b"x-secret", b"sensitive\x00"),
        (b"x-secret", b"sensitive\x7f"),
    ):
        prepared.clear()
        response = PlainTextResponse(
            "sensitive body", headers={"x-before": "sensitive"}
        )
        response.raw_headers.append((name, value))
        async with wire_server(response, buffered=buffered, prepare=prepare) as server:
            raw = await raw_request(server)
        assert raw.startswith(b"HTTP/1.1 502 "), (name, raw)
        assert prepared == [502], "The ASGI response reached prepare before rejection"
        assert (
            b"sensitive" not in raw and b"secret" not in raw and b"Injected" not in raw
        )
        assert not header_values(raw, b"x-before"), raw
        assert raw.partition(b"\r\n\r\n")[2] == b"Unsupported ASGI response header", raw


async def host_headers(buffered: bool) -> None:
    """The adapter must leave aiohttp/host-produced Unicode headers alone."""

    async def prepare(request: web.Request, response: web.StreamResponse) -> None:
        response.headers["X-Host"] = "caf\u00e9-\u2603"

    async with wire_server(
        PlainTextResponse("ok"), buffered=buffered, prepare=prepare
    ) as server:
        raw = await raw_request(server)
    assert header_values(raw, b"x-host") == ["caf\u00e9-\u2603".encode("utf-8")], raw


async def response_producers(buffered: bool) -> None:
    """Exercise actual OAuth/Starlette producers, including a Unicode URL/cookie."""
    from pydantic import AnyHttpUrl

    from ha_mcp._vendor.mcp.server.auth.middleware.bearer_auth import (
        RequireAuthMiddleware,
    )

    redirect = RedirectResponse(
        "https://example.test/caf\u00e9?state=opaque",
        headers={"Cache-Control": "no-store"},
    )
    redirect.set_cookie("example", "caf\u00e9", httponly=True, samesite="lax")
    producers: list[Any] = [
        RequireAuthMiddleware(
            PlainTextResponse("unreachable"),
            [],
            AnyHttpUrl("https://example.test/resource"),
        ),
        redirect,
        JSONResponse(
            {"label": "caf\u00e9"},
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        ),
    ]
    for producer in producers:
        observed: list[tuple[bytes, bytes]] = []

        async def app(
            scope: dict[str, Any],
            receive: Any,
            send: Any,
            *,
            producer: Any = producer,
            observed: list[tuple[bytes, bytes]] = observed,
        ) -> None:
            async def capture(message: Any) -> None:
                if message["type"] == "http.response.start":
                    observed.extend(message["headers"])
                await send(message)

            await producer(scope, receive, capture)

        async with wire_server(app, buffered=buffered) as server:
            raw = await raw_request(server)
        assert observed
        for name, value in observed:
            assert value in header_values(raw, name.lower()), (name, value, raw)


async def mcp_headers(buffered: bool) -> None:
    from ha_mcp._vendor.fastmcp import FastMCP

    mcp = FastMCP("Header compatibility regression")

    @mcp.tool
    async def echo(value: str) -> dict[str, str]:
        return {"value": value}

    app = mcp.http_app(path="/", stateless_http=True, host_origin_protection=False)
    observed: list[tuple[bytes, bytes]] = []

    async def capture_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        async def capture(message: Any) -> None:
            if message["type"] == "http.response.start":
                observed.extend(message["headers"])
            await send(message)

        await app(scope, receive, capture)

    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"value": "caf\u00e9"}},
        }
    ).encode()
    async with app.lifespan(app), wire_server(capture_app, buffered=buffered) as server:
        raw = await raw_request(
            server,
            b"Content-Type: application/json\r\nAccept: application/json, text/event-stream\r\n",
            "POST",
            payload,
        )
    assert raw.startswith(b"HTTP/1.1 200 "), raw
    assert b"structuredContent" in raw, raw
    assert observed
    for name, value in observed:
        assert value in header_values(raw, name.lower()), (name, value, raw)


async def run(case: str) -> None:
    name, mode = case.rsplit("_", 1)
    await {
        "conditional": conditional_headers,
        "rejected": rejected_headers,
        "host": host_headers,
        "producers": response_producers,
        "mcp": mcp_headers,
    }[name](mode == "buffered")
