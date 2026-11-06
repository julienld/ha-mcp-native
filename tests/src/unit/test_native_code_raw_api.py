"""Saved-script raw helpers use owned Engine calls and retain their authority."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tool_api import CallContext, ToolDefinition, ToolFailure, ToolResult

from .test_native_code import runtime


async def raw_runtime(tmp_path, monkeypatch):
    manager, code = await runtime(tmp_path, monkeypatch)
    execute = manager.backend.execute
    raw = AsyncMock(return_value={"entity_id": "sun.sun", "future": {"x": [1]}})

    async def dispatch(operation, arguments, actor):
        if operation == "code.run":
            return await execute(operation, arguments, actor)
        return await raw(operation, arguments, actor)

    manager.backend.execute = dispatch
    manager.visibility = SimpleNamespace(before=AsyncMock())
    for name in ("ha_get_state", "ha_get_system_health", "ha_list_services"):
        manager.engine.register(
            ToolDefinition(name, "Read", {}, AsyncMock(), {"readOnlyHint": True})
        )
    return manager, code, raw


async def run(manager, source, actor=None):
    return await manager.engine.call(
        "ha_manage_custom_tool",
        {"code": source, "justification": "Read a native API owner"},
        actor or CallContext("owner", "client"),
    )


async def test_raw_state_script_uses_semantic_tool_admission_and_current_actor(
    tmp_path, monkeypatch
):
    manager, _code, raw = await raw_runtime(tmp_path, monkeypatch)
    result = await run(manager, "await api_get('/api/states/sun.sun')")
    assert result.data["data"]["result"]["future"] == {"x": [1]}
    assert raw.await_count == 1
    assert raw.call_args.args[2].user_id == "owner"
    child = result.data["data"]["operations"][0]
    assert child["tool"] == "ha_get_state"
    assert child["operation_id"] in manager.engine.operations
    assert manager.visibility.before.await_count >= 1


@pytest.mark.parametrize("boundary", ["disabled", "allowed", "exposure"])
async def test_raw_state_cannot_bypass_the_ordinary_state_tool_boundary(
    tmp_path, monkeypatch, boundary
):
    manager, _code, raw = await raw_runtime(tmp_path, monkeypatch)
    actor = CallContext("owner")
    if boundary == "disabled":
        manager.engine.enabled_tools = {"ha_manage_custom_tool"}
    elif boundary == "allowed":
        actor = CallContext("owner", allowed_tools=frozenset({"ha_manage_custom_tool"}))
    else:

        def guard(name):
            if name == "ha_get_state":
                raise ToolFailure(
                    "TOOL_DISABLED", "State reads hidden", status="not_started"
                )

        actor = CallContext("owner", tool_guard=guard)
    result = await run(manager, "await api_get('/states/sun.sun')", actor)
    assert result.data["data"]["result"]["error"]["code"] == "TOOL_DISABLED"
    raw.assert_not_awaited()


async def test_hiding_activated_mid_script_refuses_the_next_raw_collection_read(
    tmp_path, monkeypatch
):
    manager, _code, raw = await raw_runtime(tmp_path, monkeypatch)
    manager.visibility.before.side_effect = ToolFailure(
        "ENTITY_VISIBILITY_ENFORCED",
        "Raw reads cannot inspect hidden entities",
        status="not_started",
    )
    result = await run(manager, "await api_get('/states')")
    assert (
        result.data["data"]["result"]["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"
    )
    raw.assert_not_awaited()


async def test_unported_script_endpoint_refuses_before_backend_dispatch(
    tmp_path, monkeypatch
):
    manager, _code, raw = await raw_runtime(tmp_path, monkeypatch)
    result = await run(manager, "await api_post('/custom_extension/action', {'x': 1})")
    assert result.data["data"]["result"]["error"]["code"] == "UNSUPPORTED_OPERATION"
    raw.assert_not_awaited()


async def test_script_service_error_after_effect_is_owned_once_and_not_replayed(
    tmp_path, monkeypatch
):
    manager, _code, raw = await raw_runtime(tmp_path, monkeypatch)
    effect = AsyncMock(side_effect=RuntimeError("effect already happened"))
    manager.engine.register(ToolDefinition("ha_call_service", "Call", {}, effect))
    result = await run(
        manager,
        "await api_post('/services/light/turn_on', {'entity_id': 'light.a'})\n42",
    )
    effect.assert_awaited_once()
    assert effect.call_args.args[0]["domain"] == "light"
    assert effect.call_args.args[0]["service"] == "turn_on"
    assert result.is_error and result.status == "outcome_unknown"
    assert result.data["data"]["result"] == 42
    assert result.data["data"]["operations"][0]["status"] == "outcome_unknown"
    raw.assert_not_awaited()


async def test_raw_service_uses_domain_policy_arguments_and_pending_has_no_effect(
    tmp_path, monkeypatch
):
    manager, _code, raw = await raw_runtime(tmp_path, monkeypatch)
    effect = AsyncMock()
    manager.engine.register(ToolDefinition("ha_call_service", "Call", {}, effect))
    observed = []

    async def policy(name, arguments, actor, writes):
        if name == "ha_call_service":
            observed.append(
                (arguments["domain"], arguments["service"], actor.user_id, writes)
            )
            return ToolResult(
                {"success": False, "error": {"code": "USER_APPROVAL_REQUIRED"}},
                is_error=True,
                status="pending",
            )
        return None

    manager.engine.policy_guard = policy
    result = await run(
        manager, "await api_post('/services/light/turn_on', {'entity_id': 'light.a'})"
    )
    assert result.data["data"]["result"]["error"]["code"] == "USER_APPROVAL_REQUIRED"
    assert observed == [("light", "turn_on", "owner", True)]
    effect.assert_not_awaited()
    raw.assert_not_awaited()
