"""A native approval belongs to one principal and authorizes one execution."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools.native_approvals import NativeApprovalGate  # noqa: E402
from ha_mcp.policy.model import Policy, Rule  # noqa: E402
from ha_mcp.runtime import Engine  # noqa: E402
from ha_mcp.tool_api import CallContext, ToolDefinition  # noqa: E402


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
