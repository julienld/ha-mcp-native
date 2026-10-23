"""A connection's read-only scope governs native listing and direct calls."""

import json
from contextvars import ContextVar
from types import MethodType, SimpleNamespace

import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools import native_server  # noqa: E402
from custom_components.ha_mcp_tools.native_server_policy import (  # noqa: E402
    NativePolicy,
    catalogue_middleware,
    catalogue_transform,
)
from ha_mcp._vendor.fastmcp import Client, FastMCP  # noqa: E402
from ha_mcp._vendor.fastmcp.exceptions import ToolError  # noqa: E402
from ha_mcp.tool_api import CallContext, ToolDefinition  # noqa: E402


@pytest.fixture
def server(monkeypatch):
    actor = ContextVar("test_native_readonly_actor")
    monkeypatch.setattr(native_server, "current_context", actor.get)
    mcp = FastMCP("Native scoped catalogue")
    writes = []

    @mcp.tool(annotations={"readOnlyHint": True})
    async def read() -> str:
        return "read"

    @mcp.tool(annotations={"readOnlyHint": False})
    async def write() -> str:
        writes.append("write")
        return "written"

    @mcp.tool(annotations={"readOnlyHint": False})
    async def mixed(action: str) -> str:
        if action != "list":
            writes.append(action)
        return action

    definitions = {
        "read": ToolDefinition(
            "read", "Read", {}, read, annotations={"readOnlyHint": True}
        ),
        "write": ToolDefinition("write", "Write", {}, write),
        "mixed": ToolDefinition(
            "mixed",
            "Mixed",
            {},
            mixed,
            writes=lambda args: args.get("action") != "list",
        ),
    }
    manager = SimpleNamespace(
        policy=NativePolicy(), engine=SimpleNamespace(tools=definitions)
    )
    manager._search_call_effect = MethodType(
        native_server.NativeServerManager._search_call_effect, manager
    )
    mcp.add_middleware(catalogue_middleware(manager))
    mcp.add_transform(catalogue_transform(manager))
    return mcp, actor, writes


async def test_readonly_connection_hides_writers_without_hiding_mixed_reads(server):
    mcp, actor, _writes = server
    actor.set(CallContext("owner", read_only=True))
    async with Client(mcp) as client:
        assert {tool.name for tool in await client.list_tools()} == {"read", "mixed"}
        actor.set(CallContext("owner"))
        assert {tool.name for tool in await client.list_tools()} == {
            "read",
            "mixed",
            "write",
        }


async def test_readonly_direct_calls_fail_with_public_code_before_any_effect(server):
    mcp, actor, writes = server
    actor.set(CallContext("owner", read_only=True))
    async with Client(mcp) as client:
        for name, args in (("write", {}), ("mixed", {"action": "delete"})):
            with pytest.raises(ToolError) as refused:
                await client.call_tool(name, args)
            assert json.loads(str(refused.value))["error"]["code"] == "READ_ONLY_MODE"
        assert not writes
        assert not (await client.call_tool("mixed", {"action": "list"})).is_error
        actor.set(CallContext("owner"))
        assert not (await client.call_tool("write", {})).is_error
        assert writes == ["write"]
