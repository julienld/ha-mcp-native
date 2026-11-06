"""Native fixed-catalog URLs on the real HTTP adapter, without a Core install."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from ha_mcp import tool_catalog
from ha_mcp._vendor.fastmcp import Client
from ha_mcp.tool_api import CallContext, ToolDefinition

from ._native_proxy_probe import Probe


async def _runtime(probe):
    from custom_components.ha_mcp_tools.native_resources import build_instructions
    from custom_components.ha_mcp_tools.native_server_policy import (
        NativePolicy,
        catalogue_instructions_middleware,
        catalogue_middleware,
        catalogue_transform,
    )

    runtime = probe.native.NativeServerManager.__new__(probe.native.NativeServerManager)
    runtime.accepting = True
    runtime._supervisor = SimpleNamespace(done=lambda: False)
    runtime._http_tasks = set()
    runtime.http_limit, runtime.stream_seconds = 8, 20
    runtime.backend = SimpleNamespace(authorize=probe.authorize)
    runtime.engine = probe.manager.engine
    runtime.policy = NativePolicy(
        flags={"enable_tool_search": False}, tool_states={"hidden": "disabled"}
    )
    runtime.instructions_by_catalog = {
        mode: await asyncio.to_thread(
            build_instructions, runtime.policy, {"native_read", "native_write"}, active
        )
        for mode, active in (("full", False), ("search", True))
    }
    runtime.instructions = runtime.instructions_by_catalog["full"]
    runtime.refresh_policy = AsyncMock()
    runtime.mcp = probe.backend
    runtime.path = "/api/webhook/test"
    runtime.app = probe.asgi
    runtime.lifespan_state = {}

    async def identity(_request, _cfg, *, read_only=False):
        async def validate():
            if not probe.valid:
                raise PermissionError("Revoked")

        return CallContext(
            "administrator", "catalog-test", read_only=read_only, credential=validate
        )

    runtime.request_context = identity
    probe.register(
        ToolDefinition(
            "hidden",
            "Hidden secret",
            probe.schema,
            probe.read,
            annotations={"readOnlyHint": True},
        )
    )
    runtime.mcp.add_transform(catalogue_transform(runtime))
    runtime.mcp.add_middleware(catalogue_instructions_middleware(runtime))
    runtime.mcp.add_middleware(catalogue_middleware(runtime))
    runtime._configure_search()
    return runtime


async def _cloudhook_catalog(runtime, probe):
    cloud = probe.native.MockRequest()
    cloud.method = "POST"
    cloud.headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Mcp-Method": "tools/list",
    }
    cloud.content = SimpleNamespace(
        read=AsyncMock(
            return_value=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                    "params": {
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientCapabilities": {},
                        }
                    },
                }
            ).encode()
        )
    )
    buffered = await runtime.async_handle_http(
        cloud, {}, read_only=True, catalog_mode="search"
    )
    assert buffered.status == 200, buffered.body
    wire = buffered.body.decode()
    body = (
        json.loads(wire)
        if wire.startswith("{")
        else next(
            json.loads(line[5:].strip())
            for line in wire.splitlines()
            if line.startswith("data:")
        )
    )
    assert {t["name"] for t in body["result"]["tools"]} == {
        "ha_search_tools",
        "ha_call_read_tool",
    }, body


async def run() -> None:
    probe = Probe("catalog")
    # A legacy server's unavailable search must not control a native entry.
    tool_catalog.set_search_available(False)
    runtime = await _runtime(probe)

    async def handle(request):
        if request.headers.get("Authorization") != "Bearer test-credential":
            return web.Response(status=401)
        suffix = request.path.removeprefix(runtime.path)
        mode = suffix.rsplit("/", 1)[-1] if "/catalog/" in suffix else None
        return await runtime.async_handle_http(
            request, {}, read_only=suffix.startswith("/readonly"), catalog_mode=mode
        )

    async with probe.asgi.lifespan(probe.asgi) as state:
        runtime.lifespan_state = dict(state or {})
        app = web.Application()
        app.router.add_route("*", runtime.path + "{tail:.*}", handle)
        async with TestServer(app) as server:

            def client(suffix, *, mode="auto"):
                return Client(
                    str(server.make_url(runtime.path + suffix)),
                    auth="test-credential",
                    timeout=15,
                    mode=mode,
                )

            async with (
                client("/catalog/full") as full,
                client("/catalog/search") as search,
                client("/readonly/catalog/full") as readonly,
                client("/readonly/catalog/search") as readonly_search,
            ):

                async def names(c):
                    return {t.name for t in await c.list_tools()}

                f, s, r = await asyncio.gather(
                    names(full), names(search), names(readonly)
                )
                assert f == {"native_read", "native_write"}, f
                assert (
                    "ha_search_tools" in s
                    and "native_read" not in s
                    and "hidden" not in s
                ), s
                assert r == {"native_read"}, r
                assert full.initialize_result is None
                assert search.initialize_result is None
                assert full.instructions == runtime.instructions_by_catalog["full"]
                assert search.instructions == runtime.instructions_by_catalog["search"]
                assert full.instructions != search.instructions
                assert await names(readonly_search) == {
                    "ha_search_tools",
                    "ha_call_read_tool",
                }
                for _ in range(3):
                    writable, limited = await asyncio.gather(
                        search.call_tool("ha_search_tools", {"query": "native"}),
                        readonly_search.call_tool(
                            "ha_search_tools", {"query": "native"}
                        ),
                    )
                    assert "native_write" in json.dumps(writable.data)
                    assert "native_write" not in json.dumps(limited.data)
                    assert "hidden" not in json.dumps(writable.data)
                refused_proxy = await readonly_search.call_tool(
                    "ha_call_write_tool",
                    {"name": "native_write", "arguments": {}},
                    raise_on_error=False,
                )
                assert refused_proxy.is_error and not probe.mutations
                refused = await readonly.call_tool(
                    "native_write", {}, raise_on_error=False
                )
                assert refused.is_error and not probe.mutations
                assert (
                    await full.call_tool("native_read", {"value": "kept"})
                ).data == {"value": "kept", "actor": "administrator"}
                assert await names(full) == f
                probe.valid = False
                async with (
                    ClientSession() as session,
                    session.post(
                        server.make_url(runtime.path + "/catalog/full"),
                        headers={"Authorization": "Bearer test-credential"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    ) as revoked,
                ):
                    assert revoked.status == 403
                probe.valid = True
            async with client("") as default:
                assert await names(default) == {"native_read", "native_write"}
                assert default.instructions == runtime.instructions_by_catalog["full"]
            for suffix, mode in (
                ("/catalog/full", "full"),
                ("/catalog/search", "search"),
            ):
                async with client(suffix, mode="legacy") as legacy:
                    assert legacy.initialize_result is not None
                    assert legacy.instructions == runtime.instructions_by_catalog[mode]

            await _cloudhook_catalog(runtime, probe)
    await runtime.engine.stop()
