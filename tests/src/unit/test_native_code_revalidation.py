"""Code mode cannot return saved scripts after authority changes during I/O."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tool_api import CallContext

from .test_native_code import runtime


async def test_sandbox_compilation_leaves_the_loop_but_external_callbacks_stay_on_it():
    import asyncio
    import threading
    from types import SimpleNamespace

    from ha_mcp.runtime.code_runner import run_sandboxed_code

    owner_loop, owner_thread = asyncio.get_running_loop(), threading.get_ident()

    class Compiler:
        def __init__(self, code, **kwargs):
            assert threading.get_ident() != owner_thread, (
                "compilation blocks the HA loop"
            )

        async def run_async(self, **kwargs):
            assert asyncio.get_running_loop() is owner_loop
            return await kwargs["external_functions"]["call_tool"]("read", {})

    async def callback(name, args):
        assert asyncio.get_running_loop() is owner_loop
        return {"actual_loop": True}

    settings = SimpleNamespace(
        code_mode_max_duration=1, code_mode_max_memory=1024, code_mode_max_recursion=10
    )
    result = await run_sandboxed_code(
        "fixture", {"call_tool": callback}, settings, Compiler, lambda **kwargs: kwargs
    )
    assert result == {"actual_loop": True}


@pytest.mark.parametrize("mutation", [False, True])
async def test_native_code_revalidates_after_registry_io(
    tmp_path, monkeypatch, mutation
):
    manager, code = await runtime(tmp_path, monkeypatch, persistent=True)
    registry_call = code._registry_call
    revoked = [False]

    async def authorize(_actor):
        if revoked[0]:
            raise PermissionError("credential revoked")

    async def revoke_after_io(method, *args, **kwargs):
        result = await registry_call(method, *args, **kwargs)
        revoked[0] = True
        return result

    manager.backend.authorize = AsyncMock(side_effect=authorize)
    monkeypatch.setattr(code, "_registry_call", revoke_after_io)
    arguments = (
        {"code": "40 + 2", "justification": "Contract check", "save_as": "answer"}
        if mutation
        else {"list_saved": True}
    )
    result = await manager.engine.call(
        "ha_manage_custom_tool", arguments, CallContext("owner")
    )
    assert result.is_error
    assert result.data["error"]["code"] == "AUTH_FORBIDDEN"
    assert result.status == ("outcome_unknown" if mutation else "not_started")
    assert "saved_tools" not in repr(result.data)
    if mutation:
        assert code.registry.tools["answer"]["code"] == "40 + 2"
        assert result.data["saved_registry_changed"] is True


@pytest.mark.parametrize("child_status", ["confirmed", "outcome_unknown"])
@pytest.mark.parametrize("max_records", [1, 128])
async def test_save_refusal_keeps_prior_child_effects(
    tmp_path, monkeypatch, child_status, max_records
):
    from ha_mcp.tool_api import ToolDefinition, ToolResult

    manager, code = await runtime(tmp_path, monkeypatch)
    manager.engine.max_records = max_records
    effects = []

    async def mutate(_arguments, _actor):
        effects.append("once")
        manager.policy = replace(manager.policy, read_only=True)
        return ToolResult(
            {"changed": True},
            is_error=child_status == "outcome_unknown",
            status=child_status,
        )

    manager.engine.register(ToolDefinition("write", "Write", {}, mutate))
    result = await manager.engine.call(
        "ha_manage_custom_tool",
        {
            "code": "await call_tool('write', {})",
            "justification": "Exercise an effect before a refused save",
            "save_as": "after_effect",
        },
        CallContext("owner"),
    )
    assert result.is_error and result.status == "outcome_unknown", result
    assert result.data["error"]["code"] == "READ_ONLY"
    assert result.data["operations"][0]["tool"] == "write"
    assert result.data["operations"][0]["status"] == child_status
    assert effects == ["once"] and "after_effect" not in code.registry.tools


async def test_save_refusal_after_read_does_not_invent_a_write(tmp_path, monkeypatch):
    from ha_mcp.tool_api import ToolDefinition

    manager, _ = await runtime(tmp_path, monkeypatch)

    async def read(_arguments, _actor):
        manager.policy = replace(manager.policy, read_only=True)
        return {"value": "ready"}

    manager.engine.register(
        ToolDefinition("read", "Read", {}, read, {"readOnlyHint": True})
    )
    result = await manager.engine.call(
        "ha_manage_custom_tool",
        {
            "code": "await call_tool('read', {})",
            "justification": "Read then refuse saving",
            "save_as": "read_only",
        },
        CallContext("owner"),
    )
    assert result.is_error and result.status == "not_started", result
    assert result.data["error"]["code"] == "READ_ONLY"
    assert result.data["operations"][0]["status"] == "confirmed"


@pytest.mark.parametrize("failure_kind", ["tool", "permission"])
async def test_session_failure_rechecks_authority_before_exposing_details(
    tmp_path, monkeypatch, failure_kind
):
    from ha_mcp.tool_api import ToolDefinition, ToolFailure

    manager, code = await runtime(tmp_path, monkeypatch)
    effect = AsyncMock(return_value={"secret_child_result": "private"})
    manager.engine.register(ToolDefinition("write", "Write", {}, effect))
    registry_call = code._registry_call

    async def revoke_before_save(method, *args, **kwargs):
        if method == "save_as":
            manager.backend.authorize.side_effect = PermissionError("revoked")
            if failure_kind == "permission":
                raise PermissionError("SECRET_SESSION_ERROR")
            raise ToolFailure("READ_ONLY", "SECRET_SESSION_ERROR", status="not_started")
        return await registry_call(method, *args, **kwargs)

    monkeypatch.setattr(code, "_registry_call", revoke_before_save)
    result = await manager.engine.call(
        "ha_manage_custom_tool",
        {
            "code": "await call_tool('write', {})",
            "justification": "Exercise revoked result boundary",
            "save_as": "refused",
        },
        CallContext("owner"),
    )
    assert result.is_error and result.status == "outcome_unknown", result
    assert result.data["error"]["code"] == "AUTH_FORBIDDEN"
    assert "SECRET_SESSION_ERROR" not in str(result.data)
    assert "secret_child_result" not in str(result.data)
    effect.assert_awaited_once()
