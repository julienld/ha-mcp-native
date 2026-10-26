"""Isolated real aiohttp/FastMCP probes, unaffected by this tier's HA stubs."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


def adapter() -> Any:
    path = (
        Path(__file__).resolve().parents[3]
        / "custom_components/ha_mcp_tools/http_adapter.py"
    )
    spec = importlib.util.spec_from_file_location("native_http_adapter_probe", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def streaming() -> None:
    module = adapter()
    release = asyncio.Event()
    observed = {}

    async def asgi(scope: dict, receive: Any, send: Any) -> None:
        observed.update(scope)
        observed["body"] = await receive()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/event-stream"),
                    (b"set-cookie", b"a=1"),
                    (b"set-cookie", b"b=2"),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"data: first\n\n",
                "more_body": True,
            }
        )
        await release.wait()
        await send({"type": "http.response.body", "body": b"data: last\n\n"})

    async def handle(request: web.Request) -> web.StreamResponse:
        return await module.serve_asgi(
            asgi,
            request,
            lifespan_state={"live": 1},
            request_state={"identity": "trusted"},
        )

    app = web.Application()
    app.router.add_post("/api/webhook/test", handle)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/webhook/test?q=a%2Fb",
            data=b"input",
            headers={"X-Identity": "attacker"},
        )
        assert await response.content.readline() == b"data: first\n"
        assert response.headers.getall("Set-Cookie") == ["a=1", "b=2"]
        assert observed["state"] == {"live": 1, "identity": "trusted"}
        assert observed["query_string"] == b"q=a/b"
        assert observed["body"]["body"] == b"input"
        release.set()
        assert b"last" in await response.read()


async def oversized() -> None:
    module = adapter()
    called = False

    async def asgi(*_args: Any) -> None:
        nonlocal called
        called = True

    async def handle(request: web.Request) -> web.StreamResponse:
        return await module.serve_asgi(asgi, request, max_body_bytes=4)

    async def chunks():
        yield b"123"
        yield b"456"

    app = web.Application()
    app.router.add_post("/", handle)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/", data=chunks())
        assert response.status == 413
        assert not called


async def buffered() -> None:
    module = adapter()

    async def asgi(scope: dict, receive: Any, send: Any) -> None:
        assert (await receive())["body"] == b"payload"
        await send(
            {
                "type": "http.response.start",
                "status": 202,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"ok":true}'})

    kwargs = {
        "method": "POST",
        "path": "/api/webhook/test",
        "headers": [],
        "body": b"payload",
        "state": {},
    }
    response = await module.serve_buffered_asgi(asgi, **kwargs)
    assert response.status == 202 and json.loads(response.body)["ok"]
    try:
        await module.serve_buffered_asgi(asgi, **kwargs, max_response_bytes=3)
    except web.HTTPBadGateway:
        pass
    else:
        raise AssertionError("Cloudhook response buffer did not enforce its bound")


async def fastmcp() -> None:
    from ha_mcp._vendor.fastmcp import FastMCP

    module = adapter()
    server = FastMCP("Native adapter test")

    @server.tool
    async def echo(value: str) -> dict[str, str]:
        return {"value": value}

    app = server.http_app(
        path="/api/webhook/test", stateless_http=True, host_origin_protection=False
    )
    async with app.lifespan(app) as state:

        async def handle(request: web.Request) -> web.StreamResponse:
            return await module.serve_asgi(app, request, lifespan_state=state)

        aiohttp_app = web.Application()
        aiohttp_app.router.add_post("/api/webhook/test", handle)
        async with TestClient(TestServer(aiohttp_app)) as client:
            response = await client.post(
                "/api/webhook/test",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"value": "native"}},
                },
                headers={"Accept": "application/json, text/event-stream"},
            )
            assert response.status == 200, await response.text()
            raw = await response.text()
            result = json.loads(
                next(line[6:] for line in raw.splitlines() if line.startswith("data: "))
            )
            assert result["result"]["structuredContent"] == {"value": "native"}


async def peer_closes_after_result(payload: bytes) -> None:
    """An SDK can close after its SSE result, before the empty HTTP terminator."""
    module = adapter()
    release = asyncio.Event()
    finished = asyncio.Event()
    errors = []
    requests = []

    async def asgi(scope: dict, receive: Any, send: Any) -> None:
        await receive()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'data: {"result": "ok"}\n\n',
                "more_body": True,
            }
        )
        await release.wait()
        # Establish the observed closing-transport state synchronously: otherwise
        # aiohttp's handler cancellation can win before the adapter's EOF send.
        if (transport := requests[0].transport) is not None:
            transport.close()
        await send({"type": "http.response.body", "body": payload, "more_body": False})

    async def handle(request: web.Request) -> web.StreamResponse:
        requests.append(request)
        try:
            return await module.serve_asgi(asgi, request)
        except OSError as error:
            errors.append(type(error).__name__)
            raise
        finally:
            finished.set()

    app = web.Application()
    app.router.add_post("/", handle)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/", data=b"input")
        assert await response.content.readline() == b'data: {"result": "ok"}\n'
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
    if payload:
        assert errors, "A lost payload must still surface as an incomplete response"
    else:
        assert not errors, "A peer closing after its result must not break empty EOF"


async def disconnect_during_empty_eof() -> None:
    """A disconnect during write_eof has the same semantics as before send."""
    from types import SimpleNamespace

    module = adapter()
    invocation = module._Invocation(
        SimpleNamespace(transport=SimpleNamespace(is_closing=lambda: False)), b""
    )

    async def write_eof() -> None:
        raise ConnectionResetError("Peer closed while flushing empty EOF")

    invocation.response = SimpleNamespace(write_eof=write_eof)
    await invocation.send(
        {"type": "http.response.body", "body": b"", "more_body": False}
    )
    assert invocation.complete


async def run(case: str) -> None:
    await {
        "streaming": streaming,
        "oversized": oversized,
        "buffered": buffered,
        "fastmcp": fastmcp,
        "peer_eof": lambda: peer_closes_after_result(b""),
        "peer_payload": lambda: peer_closes_after_result(b"not delivered"),
        "eof_race": disconnect_during_empty_eof,
    }[case]()
