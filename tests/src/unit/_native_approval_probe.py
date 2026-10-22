"""Wire-level native approval probe using the real FastMCP HTTP client."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer


def _component_imports() -> Any:
    root = Path(__file__).resolve().parents[3]
    package = ModuleType("custom_components.ha_mcp_tools")
    package.__path__ = [str(root / "custom_components/ha_mcp_tools")]
    sys.modules[package.__name__] = package
    for name in (
        "homeassistant",
        "homeassistant.const",
        "homeassistant.core",
        "homeassistant.util",
        "homeassistant.util.aiohttp",
    ):
        sys.modules[name] = ModuleType(name)
    sys.modules["homeassistant.core"].HassJob = object
    sys.modules["homeassistant.const"].__version__ = "2026.10.2"
    sys.modules["homeassistant.util.aiohttp"].MockRequest = type("MockRequest", (), {})
    from custom_components.ha_mcp_tools import native_server

    return native_server


def _client(url: str, identity: str, through_proxy: bool) -> Any:
    from ha_mcp._vendor.fastmcp import Client
    from ha_mcp._vendor.fastmcp.client.transports import StreamableHttpTransport

    if through_proxy:
        from ha_mcp.native_proxy import create_native_component_proxy

        return Client(create_native_component_proxy(url, auth=identity))
    return Client(StreamableHttpTransport(url, headers={"X-Test-Client": identity}))


async def run(*, through_proxy: bool = False) -> None:
    from ha_mcp._vendor.fastmcp import FastMCP
    from ha_mcp._vendor.mcp import MCPError
    from ha_mcp._vendor.mcp.server.request_state import RequestStateSecurity
    from ha_mcp._vendor.mcp_types import InputRequiredResult
    from ha_mcp.policy import mrtr
    from ha_mcp.policy.model import Policy, Rule
    from ha_mcp.runtime import Engine
    from ha_mcp.tool_api import CallContext, ToolDefinition

    native = _component_imports()
    from custom_components.ha_mcp_tools.native_approvals import (
        NativeApprovalGate,
        approval_context_middleware,
    )

    mrtr.ROUND_WAIT_SECONDS = 0.01
    executed = []

    async def noop(*_args: Any) -> None:
        return None

    async def write(args: dict, actor: CallContext) -> dict:
        executed.append(actor.user_id)
        return {"saved": args["value"]}

    definition = ToolDefinition(
        "native_write",
        "Native write",
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        write,
    )
    manager = SimpleNamespace(
        refresh_policy=noop,
        policy=SimpleNamespace(
            security_policy=Policy(rules=[Rule(tool_name="native_write")])
        ),
        hass=SimpleNamespace(bus=SimpleNamespace(async_fire=lambda *_args: None)),
    )
    gate = NativeApprovalGate(manager)
    manager.engine = Engine(
        SimpleNamespace(authorize=noop), [definition], policy_guard=gate
    )
    mcp = FastMCP(
        "Native approval probe",
        request_state_security=RequestStateSecurity(
            keys=[b"test-key-for-native-approvals-32bytes"],
            audience="native-test",
            bind_principal=lambda _context: json.dumps(
                [native.current_context().user_id, native.current_context().client_id]
            ),
        ),
    )
    mcp.add_middleware(approval_context_middleware())
    mcp.add_tool(native._project_tool(manager, definition))
    app = mcp.http_app(
        path="/api/webhook/test", stateless_http=True, host_origin_protection=False
    )
    async with app.lifespan(app) as state:

        async def handle(request: web.Request) -> web.StreamResponse:
            actor = CallContext(
                "alice",
                request.headers.get("X-Test-Client")
                or request.headers.get("Authorization", "Bearer A").removeprefix(
                    "Bearer "
                ),
            )
            return await native.serve_asgi(
                app,
                request,
                lifespan_state=state,
                request_state={"ha_mcp_context": actor},
            )

        aiohttp_app = web.Application()
        aiohttp_app.router.add_post("/api/webhook/test", handle)
        async with TestServer(aiohttp_app) as server:
            url = str(server.make_url("/api/webhook/test"))

            async with _client(url, "A", through_proxy) as client:
                first = await client.session.call_tool(
                    "native_write", {"value": "once"}, allow_input_required=True
                )
                assert isinstance(first, InputRequiredResult), first
                assert first.request_state and not executed
                pending = gate.queue.list_pending()[0]
                assert pending.token not in first.request_state
                gate.queue.approve(pending.token)
                async with _client(url, "B", through_proxy) as other:
                    try:
                        await other.session.call_tool(
                            "native_write",
                            {"value": "once"},
                            request_state=first.request_state,
                            allow_input_required=True,
                        )
                    except MCPError:
                        pass
                    else:
                        raise AssertionError("Another client resumed the approval")
                result = await client.session.call_tool(
                    "native_write",
                    {"value": "once"},
                    request_state=first.request_state,
                    allow_input_required=True,
                )
                assert not result.is_error, result
                replay = await client.session.call_tool(
                    "native_write",
                    {"value": "once"},
                    request_state=first.request_state,
                    allow_input_required=True,
                )
                assert replay.is_error
                assert executed == ["alice"]
