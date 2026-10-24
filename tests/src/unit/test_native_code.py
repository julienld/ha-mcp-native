"""Native sandbox calls use the caller's engine and an entry-owned registry."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ._embedded_stubs import install

install()


async def runtime(tmp_path, monkeypatch, *, persistent=False):
    from custom_components.ha_mcp_tools.native_code import NativeCode
    from custom_components.ha_mcp_tools.native_server_policy import NativePolicy
    from ha_mcp.runtime import Engine

    for key in (
        "ENABLE_BETA_FEATURES",
        "ENABLE_CODE_MODE",
        "CODE_MODE_SAVED_TOOLS_PATH",
    ):
        monkeypatch.delenv(key, raising=False)
    manager = SimpleNamespace(
        policy=NativePolicy(
            flags={"enable_beta_features": True, "enable_code_mode": True}
        ),
        settings_ui=SimpleNamespace(_io=asyncio.to_thread, directory=tmp_path),
        refresh_policy=AsyncMock(),
    )
    if persistent:
        manager.policy.flags["code_mode_saved_tools_path"] = str(
            tmp_path / "saved.json"
        )
    code = NativeCode(manager)

    async def execute(_op, args, ctx):
        return await code.run(args, ctx)

    manager.backend = SimpleNamespace(authorize=AsyncMock(), execute=execute)
    manager.engine = Engine(manager.backend)
    definition = await code.async_definition()
    assert definition is not None
    manager.engine.register(definition)
    return manager, code


async def test_sandbox_uses_engine_and_cannot_launder_recursive_proxies(
    tmp_path, monkeypatch
):
    from ha_mcp.tool_api import CallContext, ToolDefinition

    manager, _code = await runtime(tmp_path, monkeypatch)
    invoked = AsyncMock(return_value={"value": "lamp"})
    manager.engine.register(
        ToolDefinition("read", "Read", {}, invoked, {"readOnlyHint": True})
    )
    actor = CallContext("alice", "client")
    result = await manager.engine.call(
        "ha_manage_custom_tool",
        {
            "code": "await call_tool('read', {})",
            "justification": "Compose reads",
        },
        actor,
    )
    assert result.data["data"]["result"] == {"value": "lamp"}
    invoked.assert_awaited_once_with({}, actor)
    blocked = await manager.engine.call(
        "ha_manage_custom_tool",
        {
            "code": "await call_tool('ha_call_write_tool', {'name': 'ha_manage_custom_tool'})",
            "justification": "Try recursive proxy",
        },
        actor,
    )
    assert (
        blocked.data["data"]["result"]["error"]["code"]
        == "AUTH_INSUFFICIENT_PERMISSIONS"
    )


async def test_saved_code_registry_belongs_to_one_entry_and_lists_in_read_only(
    tmp_path, monkeypatch
):
    from ha_mcp.tool_api import CallContext, ToolFailure

    manager, _code = await runtime(tmp_path / "one", monkeypatch)
    other, _other = await runtime(tmp_path / "two", monkeypatch)
    actor = CallContext("alice")
    saved = await manager.engine.call(
        "ha_manage_custom_tool",
        {
            "code": "40 + 2",
            "justification": "Calculate",
            "save_as": "answer",
        },
        actor,
    )
    assert saved.data["data"]["saved_as"] == "answer"
    manager.engine.read_only = True
    listed = await manager.engine.call(
        "ha_manage_custom_tool", {"list_saved": True}, actor
    )
    assert listed.data["data"]["count"] == 1
    second = await other.engine.call(
        "ha_manage_custom_tool", {"list_saved": True}, actor
    )
    assert second.data["data"]["count"] == 0
    with pytest.raises(ToolFailure, match="Writes are disabled"):
        await manager.engine.call(
            "ha_manage_custom_tool", {"run_saved": "answer"}, actor
        )


async def test_legacy_mcp_keeps_the_shared_code_schema_and_live_context(
    tmp_path, monkeypatch
):
    from ha_mcp._vendor.fastmcp import Client, FastMCP
    from ha_mcp.tools import tools_code

    manager, code = await runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(tools_code, "_saved_tools", {})
    monkeypatch.setattr(
        tools_code,
        "get_global_settings",
        lambda: SimpleNamespace(enable_code_mode=True, **vars(code.settings)),
    )
    mcp = FastMCP("Legacy code")

    @mcp.tool()
    async def read() -> dict:
        return {"value": 42}

    tools_code.register_code_tools(mcp, SimpleNamespace())
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        from ha_mcp.runtime.catalog import _without_schema_titles

        assert (
            _without_schema_titles(tools["ha_manage_custom_tool"].input_schema)
            == manager.engine.tools["ha_manage_custom_tool"].input_schema
        )
        result = await client.call_tool(
            "ha_manage_custom_tool",
            {"code": "await call_tool('read', {})", "justification": "Compose"},
        )
        assert result.data["data"]["result"] == {"value": 42}


async def test_nested_pending_does_not_capture_outer_script_for_mrtr_replay(
    tmp_path, monkeypatch
):
    from custom_components.ha_mcp_tools.native_approvals import MCP_APPROVAL_CONTEXT
    from ha_mcp.tool_api import CallContext, ToolDefinition, ToolResult

    manager, _code = await runtime(tmp_path, monkeypatch)
    effect = AsyncMock(return_value="changed")
    manager.engine.register(ToolDefinition("write", "Write", {}, effect))
    observed = []

    async def gate(name, _args, _actor, _writes):
        observed.append((name, MCP_APPROVAL_CONTEXT.get()))
        if name == "write":
            return ToolResult(
                {"success": False, "error": {"code": "USER_APPROVAL_REQUIRED"}},
                is_error=True,
                status="pending",
            )
        return None

    manager.engine.policy_guard = gate
    outer = object()
    token = MCP_APPROVAL_CONTEXT.set(outer)
    try:
        result = await manager.engine.call(
            "ha_manage_custom_tool",
            {"code": "await call_tool('write', {})", "justification": "Compose"},
            CallContext("alice"),
        )
        assert MCP_APPROVAL_CONTEXT.get() is outer
    finally:
        MCP_APPROVAL_CONTEXT.reset(token)
    assert observed == [("ha_manage_custom_tool", outer), ("write", None)]
    assert result.data["data"]["operations"][0]["status"] == "pending"
    assert result.data["data"]["result"]["error"]["code"] == "USER_APPROVAL_REQUIRED"
    effect.assert_not_awaited()


async def test_ignored_nested_uncertainty_remains_visible_on_outer_operation(
    tmp_path, monkeypatch
):
    from ha_mcp.tool_api import CallContext, ToolDefinition, ToolResult

    manager, _code = await runtime(tmp_path, monkeypatch)
    manager.engine.register(
        ToolDefinition(
            "write",
            "Write",
            {},
            AsyncMock(
                return_value=ToolResult(
                    {"error": "Disconnected"}, is_error=True, status="outcome_unknown"
                )
            ),
        )
    )
    result = await manager.engine.call(
        "ha_manage_custom_tool",
        {"code": "await call_tool('write', {})\n7", "justification": "Compose"},
        CallContext("alice"),
    )
    assert result.is_error and result.status == "outcome_unknown"
    assert result.data["data"]["result"] == 7


async def test_saved_script_reloads_legacy_format_and_executes_as_current_actor(
    tmp_path, monkeypatch
):
    import json

    from ha_mcp.tool_api import CallContext, ToolDefinition

    async def who(_arguments, actor):
        return actor.user_id

    manager, _ = await runtime(tmp_path, monkeypatch, persistent=True)
    manager.engine.register(
        ToolDefinition("who", "Identity", {}, who, {"readOnlyHint": True})
    )
    saved = await manager.engine.call(
        "ha_manage_custom_tool",
        {
            "code": "await call_tool('who', {})",
            "justification": "Read as caller",
            "save_as": "identity",
        },
        CallContext("alice"),
    )
    assert saved.data["data"]["result"] == "alice"
    payload = json.loads((tmp_path / "saved.json").read_text())
    assert payload["version"] == 1
    assert set(payload["saved_tools"]["identity"]) == {"code", "justification"}
    reloaded, _ = await runtime(tmp_path, monkeypatch, persistent=True)
    reloaded.engine.register(
        ToolDefinition("who", "Identity", {}, who, {"readOnlyHint": True})
    )
    result = await reloaded.engine.call(
        "ha_manage_custom_tool", {"run_saved": "identity"}, CallContext("bob")
    )
    assert result.data["data"]["result"] == "bob"
    deleted = await reloaded.engine.call(
        "ha_manage_custom_tool",
        {
            "code": "delete_saved_tool('identity')",
            "justification": "Remove saved script",
        },
        CallContext("bob"),
    )
    assert deleted.data["data"]["result"] == {"deleted": True, "name": "identity"}
    assert json.loads((tmp_path / "saved.json").read_text())["saved_tools"] == {}


async def test_unsupported_saved_helper_alias_is_rejected_before_earlier_script_writes(
    tmp_path, monkeypatch
):
    from ha_mcp.tool_api import CallContext, ToolDefinition

    manager, _ = await runtime(tmp_path, monkeypatch)
    effect = AsyncMock(return_value="changed")
    manager.engine.register(ToolDefinition("write", "Write", {}, effect))
    result = await manager.engine.call(
        "ha_manage_custom_tool",
        {
            "code": "await call_tool('write', {})\ndelete = delete_saved_tool\ndelete('answer')",
            "justification": "Run saved helper alias",
        },
        CallContext("alice"),
    )
    assert result.is_error and result.status == "not_started"
    assert result.data["error"]["code"] == "SANDBOX_SYNTAX_UNSUPPORTED"
    effect.assert_not_awaited()
