"""Invocation guarantees shared by HTTP, Assist and imported tools."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from typing import Any

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.runtime.catalog import build_initial_catalog
from ha_mcp.tool_api import CallContext, ToolDefinition, ToolFailure, ToolResult


class Backend:
    def __init__(self) -> None:
        self.authorizations = 0
        self.revoked = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def authorize(self, context: CallContext) -> None:
        self.authorizations += 1
        if self.revoked:
            raise PermissionError("revoked")
        if context.credential:
            await context.credential()

    async def execute(
        self, operation: str, arguments: dict[str, Any], context: CallContext
    ) -> Any:
        self.calls.append((operation, arguments))
        if operation == "instance.timezone":
            return {"timezone": "UTC"}
        if operation == "states.read":
            return {
                "states": {
                    "light.a": {
                        "entity_id": "light.a",
                        "state": "on",
                        "attributes": {"brightness": 100, "future": 3},
                    }
                }
            }
        raise AssertionError(operation)


ACTOR = CallContext("user")


def definition(handler: Any, *, read_only: bool = False) -> ToolDefinition:
    return ToolDefinition(
        "work", "work", {"type": "object"}, handler, {"readOnlyHint": read_only}
    )


async def test_disconnect_preserves_exactly_one_owned_write() -> None:
    backend = Backend()
    started, finish = asyncio.Event(), asyncio.Event()
    writes: list[int] = []

    async def write(arguments: dict, context: CallContext) -> dict:
        writes.append(1)
        started.set()
        await finish.wait()
        return {"success": True}

    engine = Engine(backend, [definition(write)])
    waiter = asyncio.create_task(engine.call("work", {}, ACTOR))
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert len(engine.tasks) == 1
    finish.set()
    await engine.stop()
    assert writes == [1]
    assert [record["status"] for record in engine.operations.values()] == ["confirmed"]


async def test_unknown_mutation_outcome_is_never_retried() -> None:
    calls = 0

    async def mutate_then_fail(arguments: dict, context: CallContext) -> None:
        nonlocal calls
        calls += 1
        raise TimeoutError("reply lost after mutation")

    engine = Engine(Backend(), [definition(mutate_then_fail)])
    result = await engine.call("work", {}, ACTOR)
    assert calls == 1
    assert result.is_error and result.status == "outcome_unknown"
    assert result.data["error"]["code"] == "OUTCOME_UNKNOWN"


@pytest.mark.parametrize("context", [ACTOR, CallContext("user", read_only=True)])
async def test_write_policy_also_applies_to_imported_tools(
    context: CallContext,
) -> None:
    calls = []

    async def mutate() -> None:
        calls.append(1)

    engine = Engine(Backend(), read_only=not context.read_only)
    with pytest.raises(ToolFailure, match="Writes are disabled"):
        await engine.call_owned("imported", mutate, context, writes=True)
    assert calls == []


async def test_identity_revalidated_immediately_before_execution() -> None:
    backend = Backend()
    executed = []

    async def revoke() -> None:
        # call(), call_owned(), then the independent owned task.
        if backend.authorizations == 3:
            backend.revoked = True
            raise PermissionError("revoked")

    async def mutate(arguments: dict, context: CallContext) -> None:
        executed.append(1)

    engine = Engine(backend, [definition(mutate)])
    result = await engine.call("work", {}, CallContext("user", credential=revoke))
    assert executed == []
    assert result.status == "not_started"


async def test_cap_prevents_unbounded_owned_work() -> None:
    finish, started = asyncio.Event(), asyncio.Event()

    async def work(arguments: dict, context: CallContext) -> None:
        started.set()
        await finish.wait()

    engine = Engine(Backend(), [definition(work)], max_operations=1)
    waiter = asyncio.create_task(engine.call("work", {}, ACTOR))
    await started.wait()
    with pytest.raises(ToolFailure, match="Too many"):
        await engine.call("work", {}, ACTOR)
    finish.set()
    await waiter
    await engine.stop()


async def test_failed_drain_keeps_owner_and_can_be_retried() -> None:
    finish, started, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def resistant(arguments: dict, context: CallContext) -> None:
        started.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await finish.wait()

    engine = Engine(
        Backend(), [definition(resistant)], drain_timeout=0, cancel_timeout=0
    )
    waiter = asyncio.create_task(engine.call("work", {}, ACTOR))
    await started.wait()
    with pytest.raises(RuntimeError, match="owned_operations_not_drained"):
        await engine.stop()
    await cancelled.wait()
    assert engine.tasks and not engine.accepting
    with pytest.raises(ToolFailure, match="stopping"):
        await engine.call("work", {}, ACTOR)
    finish.set()
    await waiter
    await engine.stop()
    assert not engine.tasks


async def test_budget_expiry_returns_unknown_write_without_retry() -> None:
    calls = []

    async def blocked(arguments: dict, context: CallContext) -> None:
        calls.append(1)
        await asyncio.Event().wait()

    engine = Engine(Backend(), [definition(blocked)], operation_timeout=0.001)
    result = await engine.call("work", {}, ACTOR)
    assert result.is_error and result.status == "outcome_unknown" and calls == [1]


async def test_results_keep_public_data_and_bounded_diagnostics() -> None:
    async def read(arguments: dict, context: CallContext) -> ToolResult:
        return ToolResult({"existing": "shape"}, status="partial")

    engine = Engine(Backend(), [definition(read, read_only=True)], max_records=2)
    for _ in range(5):
        result = await engine.call("work", {}, ACTOR)
        assert result.data == {"existing": "shape"}
        assert result.status == "partial" and result.operation_id
    assert len(engine.operations) == 2


async def test_same_state_tool_validates_bulk_bounds_and_preserves_projection() -> None:
    backend = Backend()
    engine = Engine(backend, build_initial_catalog(backend))
    result = await engine.call(
        "ha_get_state",
        {
            "entity_id": ["light.a", "missing.b", "light.a"],
            "fields": ["attributes"],
            "attribute_keys": "brightness",
        },
        ACTOR,
    )
    assert result.data["data"]["states"] == {
        "light.a": {"attributes": {"brightness": 100}}
    }
    assert result.data["data"]["partial"] is True
    assert result.data["data"]["errors"][0]["error"]["code"] == "ENTITY_NOT_FOUND"
    assert backend.calls[0] == ("states.read", {"entity_ids": ["light.a", "missing.b"]})
    backend.calls.clear()
    result = await engine.call("ha_get_state", {"entity_id": ["light.a"] * 101}, ACTOR)
    assert result.is_error
    assert backend.calls == []


async def test_argument_validation_precedes_backend_work() -> None:
    backend = Backend()
    engine = Engine(backend, build_initial_catalog(backend))
    with pytest.raises(ToolFailure, match="limit"):
        await engine.call("ha_list_services", {"limit": 201}, ACTOR)
    with pytest.raises(ToolFailure, match="extra"):
        await engine.call(
            "ha_get_state", {"entity_id": "light.a", "unknown": True}, ACTOR
        )
    assert backend.calls == []


async def test_disabled_tools_cannot_be_called_through_owned_entry() -> None:
    engine = Engine(Backend())
    with pytest.raises(ToolFailure, match="not enabled"):
        await engine.call_owned(
            "hidden",
            lambda: asyncio.sleep(0),
            CallContext("user", allowed_tools=frozenset()),
            writes=False,
        )


async def test_policy_guard_sees_validated_arguments_and_can_defer() -> None:
    backend = Backend()
    seen = []

    async def guard(
        name: str, arguments: dict, context: CallContext, writes: bool
    ) -> ToolResult:
        seen.append((name, arguments, context.user_id, writes))
        return ToolResult({"approval_required": True}, status="pending")

    engine = Engine(backend, build_initial_catalog(backend), policy_guard=guard)
    result = await engine.call("ha_list_services", {"limit": 3}, ACTOR)
    assert result.status == "pending" and result.operation_id
    assert seen[0][1]["limit"] == 3 and seen[0][1]["offset"] == 0
    assert backend.calls == []


async def test_identity_rechecked_after_policy_await_before_mutation() -> None:
    backend = Backend()
    writes = []

    async def guard(
        name: str, arguments: dict, context: CallContext, mutates: bool
    ) -> None:
        backend.revoked = True

    async def write() -> None:
        writes.append(1)

    result = await Engine(backend, policy_guard=guard).call_owned(
        "imported", write, ACTOR, writes=True, arguments={"value": 2}
    )
    assert result.status == "not_started" and writes == []


def test_shared_catalog_imports_neither_ha_nor_remote_clients() -> None:
    code = "from ha_mcp.runtime.catalog import build_initial_catalog; import sys; assert not any(k.startswith(('homeassistant', 'ha_mcp.client', 'ha_mcp.server', 'ha_mcp._vendor.fastmcp')) for k in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
