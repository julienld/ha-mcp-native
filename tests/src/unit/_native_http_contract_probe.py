"""HTTP boundary probes using real aiohttp, without component or SDK stubs."""

from __future__ import annotations

import asyncio
import importlib.abc
import sys
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from functools import partial
from types import ModuleType, SimpleNamespace
from typing import Any, cast

from aiohttp import ClientPayloadError, web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from ._native_http_probe import adapter


async def independent_import() -> None:
    class RejectHostImports(importlib.abc.MetaPathFinder):
        def find_spec(
            self, fullname: str, path: Any = None, target: ModuleType | None = None
        ) -> None:
            if fullname.split(".", maxsplit=1)[0] in {
                "homeassistant",
                "ha_mcp",
                "fastmcp",
                "mcp",
                "starlette",
            }:
                raise AssertionError(f"Adapter depends on {fullname}")

    guard = RejectHostImports()
    sys.meta_path.insert(0, guard)
    try:
        module = adapter()
        assert callable(module.serve_asgi)
        assert callable(module.serve_buffered_asgi)
    finally:
        sys.meta_path.remove(guard)


async def scope_and_context(proxy: bool) -> None:
    module = adapter()
    context = ContextVar[str]("adapter-test-context")
    inherited = {"shared": "lifespan", "identity": "old"}
    trusted = {"identity": "trusted"}
    observed: dict[str, Any] = {}

    async def asgi(scope: dict[str, Any], receive: Any, send: Any) -> None:
        observed.update(scope)
        observed["context"] = context.get()
        assert (await receive())["body"] == b"input"
        scope["state"]["local"] = True
        await send({"type": "http.response.start", "status": 207})
        await send({"type": "http.response.body", "body": b"ok"})

    async def handle(request: web.Request) -> web.StreamResponse:
        token = context.set("request-context")
        try:
            if proxy:
                request = request.clone(remote="198.51.100.20", scheme="https")
            return cast(
                web.StreamResponse,
                await module.serve_asgi(
                    asgi,
                    request,
                    lifespan_state=inherited,
                    request_state=trusted,
                    path="/logical" if proxy else None,
                ),
            )
        finally:
            context.reset(token)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    async with TestClient(TestServer(app)) as client:
        response = await client.patch(
            URL("/items/a%2Fb?q=a%2Fb", encoded=True),
            data=b"input",
            headers=[
                ("X-Identity", "attacker"),
                ("X-Multi", "a"),
                ("X-Multi", "b"),
                ("X-Forwarded-For", "192.0.2.99"),
            ],
        )
        assert response.status == 207 and await response.read() == b"ok"
        assert observed["server"] == ("127.0.0.1", client.server.port)
    assert observed["method"] == "PATCH"
    assert observed["http_version"] == "1.1"
    assert observed["query_string"] == b"q=a%2Fb"
    assert observed["path"] == ("/logical" if proxy else "/items/a/b")
    assert observed["raw_path"] == (b"/logical" if proxy else b"/items/a%2Fb")
    assert observed["scheme"] == ("https" if proxy else "http")
    assert observed["client"][0] == ("198.51.100.20" if proxy else "127.0.0.1")
    assert (observed["client"][1] == 0) is proxy
    assert [v for k, v in observed["headers"] if k == b"x-multi"] == [b"a", b"b"]
    assert observed["state"]["identity"] == "trusted"
    assert observed["context"] == "request-context"
    assert inherited == {"shared": "lifespan", "identity": "old"}
    assert trusted == {"identity": "trusted"}


async def body_bounds(size: int, maximum: int) -> None:
    module = adapter()
    received: list[bytes] = []

    async def asgi(scope: dict[str, Any], receive: Any, send: Any) -> None:
        received.append((await receive())["body"])
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body"})

    async def handle(request: web.Request) -> web.StreamResponse:
        return cast(
            web.StreamResponse,
            await module.serve_asgi(asgi, request, max_body_bytes=maximum),
        )

    app = web.Application()
    app.router.add_post("/", handle)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/", data=b"x" * size)
        await response.read()
        if size > maximum:
            assert response.status == 413 and not received
            assert response.headers["Connection"] == "close"
        else:
            assert response.status == 200 and received == [b"x" * size]


async def framing(method: str, status: int) -> None:
    module = adapter()

    async def asgi(scope: dict[str, Any], receive: Any, send: Any) -> None:
        assert scope["method"] == method
        headers = [(b"allow", b"GET, HEAD")]
        if status != 204:
            headers += [(b"content-length", b"6"), (b"transfer-encoding", b"gzip")]
        await send(
            {"type": "http.response.start", "status": status, "headers": headers}
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"" if status == 204 else b"abc",
                "more_body": True,
            }
        )
        await send(
            {"type": "http.response.body", "body": b"" if status == 204 else b"def"}
        )

    async def handle(request: web.Request) -> web.StreamResponse:
        return cast(web.StreamResponse, await module.serve_asgi(asgi, request))

    app = web.Application()
    app.router.add_route("*", "/", handle)
    async with TestClient(TestServer(app)) as client:
        response = await client.request(method, "/")
        assert response.status == status
        assert response.headers["Allow"] == "GET, HEAD"
        assert "Transfer-Encoding" not in response.headers
        assert await response.read() == (
            b"" if method == "HEAD" or status == 204 else b"abcdef"
        )
        if status == 204:
            assert "Content-Length" not in response.headers
        else:
            assert response.headers["Content-Length"] == "6"


async def disconnect_receive(deliver_first: bool) -> None:
    module = adapter()
    observed: list[dict[str, Any]] = []
    finished = asyncio.Event()
    current: list[web.Request] = []

    async def asgi(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if deliver_first:
            assert (await receive())["body"] == b"input"
        transport = current[0].transport
        assert transport is not None
        transport.close()
        observed.append(await receive())

    async def handle(request: web.Request) -> web.StreamResponse:
        current.append(request)
        try:
            return cast(web.StreamResponse, await module.serve_asgi(asgi, request))
        except RuntimeError:
            return web.Response(status=500)
        finally:
            finished.set()

    app = web.Application()
    app.router.add_post("/", handle)
    async with TestServer(app) as server:
        reader, writer = await asyncio.open_connection(server.host, server.port)
        try:
            writer.write(
                b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 5\r\n\r\ninput"
            )
            await writer.drain()
            await asyncio.wait_for(finished.wait(), 2)
            assert await asyncio.wait_for(reader.read(), 2) == b""
        finally:
            writer.close()
            await writer.wait_closed()
    assert observed == [{"type": "http.disconnect"}]


async def eof_receive_and_late_send() -> None:
    module = adapter()
    finished = asyncio.Event()
    errors: list[BaseException] = []

    async def asgi(scope: dict[str, Any], receive: Any, send: Any) -> None:
        assert (await receive())["type"] == "http.request"
        waiter = asyncio.create_task(receive())
        try:
            await asyncio.sleep(0)
            assert not waiter.done(), "Request body EOF must not signal peer loss"
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b"complete"})
            assert await asyncio.wait_for(waiter, 2) == {"type": "http.disconnect"}
            for payload in (b"", b"late"):
                try:
                    await send({"type": "http.response.body", "body": payload})
                except ConnectionResetError:
                    pass
                else:
                    raise AssertionError("Adapter accepted a write after EOF")
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    async def handle(request: web.Request) -> web.StreamResponse:
        try:
            return cast(web.StreamResponse, await module.serve_asgi(asgi, request))
        except (AssertionError, RuntimeError) as error:
            errors.append(error)
            raise
        finally:
            finished.set()

    app = web.Application()
    app.router.add_post("/", handle)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/")
        assert await response.read() == b"complete"
        await asyncio.wait_for(finished.wait(), 2)
    assert not errors, errors


async def cancellation() -> None:
    module = adapter()
    waiting = asyncio.Event()
    cancelled = asyncio.Event()
    finished = asyncio.Event()
    handler_tasks: list[asyncio.Task[Any]] = []

    async def asgi(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await receive()
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        waiting.set()
        try:
            await receive()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def handle(request: web.Request) -> web.StreamResponse:
        task = asyncio.current_task()
        assert task is not None
        handler_tasks.append(task)
        try:
            return cast(web.StreamResponse, await module.serve_asgi(asgi, request))
        finally:
            finished.set()

    app = web.Application()
    app.router.add_post("/", handle)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/")
        assert await response.content.readexactly(5) == b"first"
        await asyncio.wait_for(waiting.wait(), 2)
        response.close()
        await asyncio.wait_for(cancelled.wait(), 2)
        await asyncio.wait_for(finished.wait(), 2)
    assert all(task.done() for task in handler_tasks)


async def incomplete_response() -> None:
    module = adapter()
    failed = asyncio.Event()

    async def asgi(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200})
        await send(
            {"type": "http.response.body", "body": b"partial", "more_body": True}
        )

    async def handle(request: web.Request) -> web.StreamResponse:
        try:
            return cast(web.StreamResponse, await module.serve_asgi(asgi, request))
        except RuntimeError:
            failed.set()
            raise

    app = web.Application()
    app.router.add_get("/", handle)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/")
        try:
            await response.read()
        except ClientPayloadError:
            pass
        else:
            raise AssertionError("A partial response became a successful EOF")
        await asyncio.wait_for(failed.wait(), 2)


async def write_backpressure() -> None:
    """A held writer must suspend ASGI send; no detached write task may bypass it."""
    module = adapter()
    entered = asyncio.Event()
    release = asyncio.Event()
    invocation = module._Invocation(
        SimpleNamespace(transport=SimpleNamespace(is_closing=lambda: False)), b""
    )

    async def write(payload: bytes) -> None:
        assert payload == b"payload"
        entered.set()
        await release.wait()

    invocation.response = SimpleNamespace(write=write)
    task = asyncio.create_task(
        invocation.send(
            {"type": "http.response.body", "body": b"payload", "more_body": True}
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert not task.done(), "ASGI send bypassed the writer's backpressure"
        release.set()
        await asyncio.wait_for(task, 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def buffered_cleanup() -> None:
    module = adapter()
    receives: list[Any] = []
    trusted = {"identity": "trusted"}

    async def asgi(scope: dict[str, Any], receive: Any, send: Any) -> None:
        assert (await receive())["body"] == b"input"
        assert scope["client"] is None and scope["server"] is None
        scope["state"]["identity"] = "changed"
        receives.append(receive)
        await send({"type": "http.response.start", "status": 200})
        await send(
            {"type": "http.response.body", "body": b"partial", "more_body": True}
        )

    try:
        await module.serve_buffered_asgi(
            asgi, method="POST", path="/", headers=[], body=b"input", state=trusted
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("Buffered adapter accepted an unfinished reply")
    assert await asyncio.wait_for(receives[0](), 2) == {"type": "http.disconnect"}
    assert trusted == {"identity": "trusted"}


async def run(case: str) -> None:
    cases: dict[str, Callable[[], Awaitable[None]]] = {
        "independent_import": independent_import,
        "scope_direct": partial(scope_and_context, False),
        "scope_proxy": partial(scope_and_context, True),
        "body_declared_oversized": partial(body_bounds, 5, 4),
        "body_exact": partial(body_bounds, 4, 4),
        "body_empty": partial(body_bounds, 0, 0),
        "framing_head": partial(framing, "HEAD", 200),
        "framing_no_content": partial(framing, "DELETE", 204),
        "framing_error": partial(framing, "OPTIONS", 405),
        "disconnect_before_receive": partial(disconnect_receive, False),
        "disconnect_after_receive": partial(disconnect_receive, True),
        "eof_receive_and_late_send": eof_receive_and_late_send,
        "cancellation": cancellation,
        "incomplete_response": incomplete_response,
        "write_backpressure": write_backpressure,
        "buffered_cleanup": buffered_cleanup,
    }
    await cases[case]()
