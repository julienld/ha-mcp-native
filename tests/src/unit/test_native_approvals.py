"""A native approval belongs to one principal and authorizes one execution."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools.native_approvals import NativeApprovalGate  # noqa: E402
from ha_mcp.policy.model import Policy, Rule  # noqa: E402
from ha_mcp.runtime import Engine  # noqa: E402
from ha_mcp.tool_api import CallContext, ToolDefinition  # noqa: E402


def test_native_decision_channel_subscribes_only_for_an_opted_in_approval(tmp_path):
    manager, _engine, _handler, _announced = runtime()
    manager.settings_ui = SimpleNamespace(directory=tmp_path)
    manager.hass.bus.async_listen = MagicMock()
    gate = manager.approval_gate
    manager.policy.security_policy = None
    gate.start_decisions()
    manager.hass.bus.async_listen.assert_not_called()

    policy = Policy(event_decisions_enabled=True)
    manager.policy.security_policy = policy
    pending = gate.queue.create("write", "digest", {}, ttl_minutes=5)
    gate._announce(pending, "write", {}, policy, CallContext("owner"), False)
    manager.hass.bus.async_listen.assert_called_once()
    # A second announcement reuses the listener and its failed-PIN budget.
    another = gate.queue.create("write", "second", {}, ttl_minutes=5)
    gate._announce(another, "write", {}, policy, CallContext("owner"), False)
    manager.hass.bus.async_listen.assert_called_once()


def runtime() -> tuple[SimpleNamespace, Engine, AsyncMock, asyncio.Queue]:
    announced: asyncio.Queue = asyncio.Queue()
    manager = SimpleNamespace(
        refresh_policy=AsyncMock(),
        policy=SimpleNamespace(
            security_policy=Policy(wait_seconds=5, rules=[Rule(tool_name="write")])
        ),
        hass=SimpleNamespace(
            bus=SimpleNamespace(
                async_fire=MagicMock(
                    side_effect=lambda _event, payload: announced.put_nowait(payload)
                )
            )
        ),
    )
    gate = NativeApprovalGate(manager)
    manager.approval_gate = gate
    handler = AsyncMock(return_value={"changed": True})
    engine = Engine(
        SimpleNamespace(authorize=AsyncMock()),
        [ToolDefinition("write", "Write", {"type": "object"}, handler)],
        policy_guard=gate,
    )
    return manager, engine, handler, announced


async def test_approval_for_one_user_cannot_release_another() -> None:
    manager, engine, handler, announced = runtime()
    alice = asyncio.create_task(
        engine.call("write", {"value": 1}, CallContext("alice", "client"))
    )
    alice_row = await announced.get()
    bob = asyncio.create_task(
        engine.call("write", {"value": 1}, CallContext("bob", "client"))
    )
    bob_row = await announced.get()
    assert alice_row["token"] != bob_row["token"]
    manager.approval_gate.queue.approve(alice_row["token"])
    manager.approval_gate.queue.deny(bob_row["token"])
    alice_result, bob_result = await asyncio.gather(alice, bob)
    assert alice_result.status == "confirmed"
    assert bob_result.status == "denied"
    handler.assert_awaited_once()
    assert handler.await_args.args[1].user_id == "alice"


async def test_approval_cannot_release_same_user_on_different_client() -> None:
    manager, engine, handler, announced = runtime()
    first = asyncio.create_task(
        engine.call("write", {}, CallContext("alice", "client-A"))
    )
    first_row = await announced.get()
    second = asyncio.create_task(
        engine.call("write", {}, CallContext("alice", "client-B"))
    )
    second_row = await announced.get()
    manager.approval_gate.queue.approve(first_row["token"])
    manager.approval_gate.queue.deny(second_row["token"])
    results = await asyncio.gather(first, second)
    assert [result.status for result in results] == ["confirmed", "denied"]
    assert handler.await_count == 1


async def test_concurrent_identical_calls_consume_approval_once() -> None:
    manager, engine, handler, announced = runtime()
    actor = CallContext("alice", "client")
    first = asyncio.create_task(engine.call("write", {}, actor))
    row = await announced.get()
    second = asyncio.create_task(engine.call("write", {}, actor))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    manager.approval_gate.queue.approve(row["token"])
    results = await asyncio.gather(first, second)
    assert sorted(result.status for result in results) == ["confirmed", "not_started"]
    assert handler.await_count == 1


async def test_policy_change_while_waiting_invalidates_approval() -> None:
    manager, engine, handler, announced = runtime()
    operation = asyncio.create_task(engine.call("write", {}, CallContext("alice")))
    row = await announced.get()
    manager.policy.security_policy = Policy(
        wait_seconds=5, rule_effect="allow", rules=[]
    )
    manager.approval_gate.queue.approve(row["token"])
    result = await operation
    assert result.status == "not_started"
    handler.assert_not_awaited()


async def test_best_practices_change_during_wait_blocks_before_execution() -> None:
    from ha_mcp.tool_api import ToolResult

    manager, engine, handler, announced = runtime()
    strict = False

    def preflight(_name: str, _arguments: dict) -> ToolResult | None:
        return (
            ToolResult(
                {"error": "acknowledgment required"},
                is_error=True,
                status="not_started",
            )
            if strict
            else None
        )

    manager.check_best_practices = preflight
    operation = asyncio.create_task(engine.call("write", {}, CallContext("alice")))
    row = await announced.get()
    strict = True
    manager.approval_gate.queue.approve(row["token"])
    result = await operation
    assert result.status == "not_started"
    handler.assert_not_awaited()


async def test_dynamic_timeout_expires_request_without_offering_a_dead_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ha_mcp.policy.approval_queue import PendingApproval

    manager, engine, handler, announced = runtime()
    name = "ha_bulk_control"
    engine.register(ToolDefinition(name, name, {}, handler))
    manager.policy.security_policy = Policy(rules=[Rule(tool_name=name)])
    monkeypatch.setattr(PendingApproval, "wait", AsyncMock(side_effect=TimeoutError))
    result = await engine.call(
        name, {"selector": {"domain": "light"}}, CallContext("alice", "assist")
    )
    token = (await announced.get())["token"]
    assert result.is_error and result.status == "not_started"
    assert result.data["error"]["code"] == "USER_APPROVAL_REQUIRED"
    assert "expired" in result.data["error"]["message"]
    assert "approval_token" not in result.data
    assert "approval_continuation" not in result.data
    assert manager.approval_gate.queue.get(token) is None
    assert not manager.approval_gate.queue.approve(token)
    handler.assert_not_awaited()


async def test_static_timeout_keeps_an_approvable_request_for_the_same_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ha_mcp.policy.approval_queue import PendingApproval

    manager, engine, handler, _announced = runtime()
    actor = CallContext("alice", "client")
    with monkeypatch.context() as timeout:
        timeout.setattr(PendingApproval, "wait", AsyncMock(side_effect=TimeoutError))
        result = await engine.call("write", {"value": 1}, actor)
    assert result.is_error and result.status == "pending"
    token = result.data["approval_token"]
    assert manager.approval_gate.queue.approve(token)
    handler.assert_not_awaited()
    resumed = await engine.call("write", {"value": 1}, actor)
    assert resumed.status == "confirmed" and not resumed.is_error
    handler.assert_awaited_once()


async def test_nested_dynamic_timeout_does_not_replay_prior_script_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from custom_components.ha_mcp_tools.native_approvals import MCP_APPROVAL_CONTEXT
    from ha_mcp.policy.approval_queue import PendingApproval

    from .test_native_code import runtime as code_runtime

    manager, _code = await code_runtime(tmp_path, monkeypatch)
    manager.hass = SimpleNamespace(bus=SimpleNamespace(async_fire=MagicMock()))
    manager.policy = replace(
        manager.policy,
        security_policy=Policy(rules=[Rule(tool_name="ha_bulk_control")]),
    )
    gate = NativeApprovalGate(manager)
    manager.engine.policy_guard = gate
    prior = AsyncMock(return_value={"changed": True})
    blocked = AsyncMock()
    manager.engine.register(ToolDefinition("write", "Write", {}, prior))
    manager.engine.register(ToolDefinition("ha_bulk_control", "Bulk", {}, blocked))
    monkeypatch.setattr(PendingApproval, "wait", AsyncMock(side_effect=TimeoutError))
    # A wire context must never turn the enclosing script into an MRTR retry.
    from .policy.test_mrtr import context

    token = MCP_APPROVAL_CONTEXT.set(context(name="ha_manage_custom_tool"))
    try:
        result = await manager.engine.call(
            "ha_manage_custom_tool",
            {
                "code": "await call_tool('write', {})\nawait call_tool('ha_bulk_control', {'selector': {'domain': 'light'}})",
                "justification": "Compose native writes",
            },
            CallContext("alice", "client"),
        )
    finally:
        MCP_APPROVAL_CONTEXT.reset(token)
    prior.assert_awaited_once()
    blocked.assert_not_awaited()
    assert not gate.queue.list_pending()
    data = result.data["data"]
    assert [operation["status"] for operation in data["operations"]] == [
        "confirmed",
        "not_started",
    ]
    assert "expired" in data["result"]["error"]["message"]
    assert "enclosing tool or script" in data["result"]["error"]["message"]
    assert "approval_token" not in data["result"]
    assert "approval_continuation" not in data["result"]
