"""Real local app server for replay, redirect and bounded stream checks."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from multidict import CIMultiDict

from ha_mcp._vendor.websockets.exceptions import WebSocketException


def transport_module() -> Any:
    path = (
        Path(__file__).resolve().parents[3]
        / "custom_components/ha_mcp_tools/native_operations/app_transport.py"
    )
    spec = importlib.util.spec_from_file_location("app_transport_probe", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def run(case: str) -> None:
    transport = transport_module()
    calls = []

    async def handler(request: web.Request) -> web.StreamResponse:
        calls.append((request.path, request.headers.get("Cookie")))
        if case in {"http_drop", "ws_drop"}:
            request.transport.close()
            return web.Response()
        if case in {"http_redirect", "ws_redirect"}:
            if request.path == "/start":
                raise web.HTTPFound("/destination")
            return web.Response(text="replayed")
        if case == "ws_command":
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            message = await socket.receive()
            calls.append(("command", message.data))
            await socket.send_str(json.dumps({"answer": 42}))
            await socket.close()
            return socket
        return web.json_response([{"id": "a"}, {"id": "b"}])

    application = web.Application()
    application.router.add_route("*", "/{path:.*}", handler)
    async with TestServer(application) as server, aiohttp.ClientSession() as session:
        route = SimpleNamespace(
            session=session,
            url=server.make_url("/start"),
            headers=CIMultiDict({"Cookie": "ingress_session=probe-private"}),
            open=AsyncMock(),
            pin=lambda: None,
            service=SimpleNamespace(admit=AsyncMock()),
        )
        await exchange(transport, route, case)
        if case == "ws_command":
            assert calls[1] == ("command", '{"command": "go"}')
        assert calls[0] == ("/start", "ingress_session=probe-private")
        assert len(calls) == (2 if case == "ws_command" else 1), calls
        assert not session.closed, "The borrowed Core session was closed"


async def exchange(transport: Any, route: Any, case: str) -> None:
    if case == "http_redirect":
        result = await transport.http_exchange(route, "GET", None)
        assert result[0] == 302
        return
    if case == "ws_command":
        result = await transport.ws_exchange(route, {"command": "go"}, None, 0, True)
        assert result[0] == ['{"answer": 42}']
        return
    try:
        if case == "http_drop":
            await transport.http_exchange(route, "GET", None)
        else:
            await transport.ws_exchange(route, {"command": "go"}, None, 0, True)
    except (RuntimeError, aiohttp.ClientError, WebSocketException):
        return
    raise AssertionError("Failed app transport was accepted")
