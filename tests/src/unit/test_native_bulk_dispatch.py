"""Core batch attempts retain ownership and never replay an uncertain provider."""

import asyncio
import importlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime.mutation_guard import MutationAdmission, current_admission
from ha_mcp.tool_api import CallContext, ToolFailure

from .test_component_ws_search import FakeState
from .test_native_engine_backend import EventBus
from .test_native_engine_backend import native as native


@pytest.fixture
def bulk(native: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.bulk_dispatch"
    )
    monkeypatch.setattr(module, "Context", SimpleNamespace)
    bus = EventBus()
    states = {f"light.item_{i}": FakeState(f"light.item_{i}", "off") for i in range(8)}
    calls = []

    async def apply(domain: str, service: str, data: dict, **kwargs: Any) -> None:
        calls.append((domain, service, data, kwargs))
        state = FakeState(data["entity_id"], "on")
        states[state.entity_id] = state
        bus.fire(state, kwargs["context"])

    services = SimpleNamespace(
        has_service=lambda *_: True, async_call=AsyncMock(side_effect=apply)
    )
    hass = SimpleNamespace(
        bus=bus,
        states=SimpleNamespace(get=states.get, async_all=lambda: list(states.values())),
        services=services,
    )
    actor = CallContext("actor", client_id="client")
    service = module.NativeBulkDispatch(
        hass,
        actor,
        authorize=AsyncMock(),
        service_policy=lambda *_: None,
        concurrency=2,
    )
    rows = [
        {
            "domain": "light",
            "service": "turn_on",
            "service_data": {"entity_id": name},
            "entity_ids": [name],
            "expected_state": "on",
        }
        for name in states
    ]
    return SimpleNamespace(
        service=service,
        rows=rows,
        bus=bus,
        calls=calls,
        apply=apply,
        services=services,
        module=module,
        actor=actor,
        hass=hass,
        states=states,
    )


async def test_batch_subscribes_all_before_core_dispatch_with_the_real_actor(
    bulk: Any,
) -> None:
    async def apply(*arguments: Any, **kwargs: Any) -> None:
        assert len(bulk.bus.listeners) == 2
        await bulk.apply(*arguments, **kwargs)

    bulk.services.async_call.side_effect = apply
    result = await bulk.service.execute(bulk.rows[:2], parallel=True, timeout=0.1)
    assert result["attempted"] == result["dispatched"] == 2
    assert all(row["confirmed"] for row in result["operations"])
    assert all(call[3]["context"].user_id == "actor" for call in bulk.calls)
    assert bulk.calls[0][3]["context"] is not bulk.calls[1][3]["context"]
    assert bulk.bus.listeners == []


async def test_every_service_guard_runs_before_any_listener_or_dispatch(
    bulk: Any,
) -> None:
    def policy(domain: str, service: str) -> None:
        if domain == "forbidden":
            raise ToolFailure(
                "READ_ONLY", "Service policy denied", status="not_started"
            )

    bulk.service.service_policy = policy
    rows = [bulk.rows[0], {**bulk.rows[1], "domain": "forbidden"}]
    with pytest.raises(ToolFailure, match="policy"):
        await bulk.service.execute(rows, parallel=False, timeout=0.1)
    bulk.services.async_call.assert_not_awaited()
    assert bulk.bus.listeners == []


async def test_provider_mutate_then_raise_is_unknown_and_stops_sequential_tail(
    bulk: Any,
) -> None:
    async def apply(*arguments: Any, **kwargs: Any) -> None:
        await bulk.apply(*arguments, **kwargs)
        raise ValueError("provider failed after applying")

    bulk.services.async_call.side_effect = apply
    result = await bulk.service.execute(bulk.rows[:2], parallel=False, timeout=0.1)
    first, second = result["operations"]
    assert first["attempted"] and first["outcome"] == "outcome_unknown"
    assert first["error_code"] == "OUTCOME_UNKNOWN" and not first["confirmed"]
    assert second["outcome"] == "not_started" and not second["attempted"]
    assert len(bulk.calls) == 1 and bulk.bus.listeners == []


async def test_provider_cancellation_is_an_unknown_attempt_not_a_safe_retry(
    bulk: Any,
) -> None:
    bulk.services.async_call.side_effect = asyncio.CancelledError("provider cancelled")
    result = await bulk.service.execute(bulk.rows[:2], parallel=False, timeout=0.1)
    assert result["operations"][0]["outcome"] == "outcome_unknown"
    assert not result["operations"][1]["attempted"]
    bulk.services.async_call.assert_awaited_once()
    assert bulk.bus.listeners == []


async def test_read_only_change_between_children_prevents_the_next_dispatch(
    bulk: Any,
) -> None:
    allowed = True

    def policy() -> None:
        if not allowed:
            raise ToolFailure("READ_ONLY", "Policy changed", status="not_started")

    async def apply(*arguments: Any, **kwargs: Any) -> None:
        nonlocal allowed
        await bulk.apply(*arguments, **kwargs)
        allowed = False

    bulk.services.async_call.side_effect = apply
    token = current_admission.set(MutationAdmission(bulk.actor, True, policy))
    try:
        result = await bulk.service.execute(bulk.rows[:3], parallel=False, timeout=0.1)
    finally:
        current_admission.reset(token)
    first, second, third = result["operations"]
    assert first["confirmed"]
    assert second["outcome"] == "not_started" and second["error_code"] == "READ_ONLY"
    assert not third["attempted"] and len(bulk.calls) == 1


async def test_parallel_failure_drains_started_owners_and_does_not_start_queued_rows(
    bulk: Any,
) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    active = 0
    peak = 0

    async def apply(*arguments: Any, **kwargs: Any) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            if arguments[2]["entity_id"] == "light.item_0":
                await started.wait()
                raise RuntimeError("first failed")
            started.set()
            await release.wait()
            await bulk.apply(*arguments, **kwargs)
        finally:
            active -= 1

    bulk.services.async_call.side_effect = apply
    task = asyncio.create_task(
        bulk.service.execute(bulk.rows, parallel=True, timeout=0.1)
    )
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.sleep(0)
    assert not task.done() and bulk.bus.listeners
    release.set()
    result = await asyncio.wait_for(task, 1)
    assert peak == 2 and active == 0 and bulk.services.async_call.await_count == 2
    assert result["operations"][0]["outcome"] == "outcome_unknown"
    assert result["operations"][1]["confirmed"]
    assert all(not row["attempted"] for row in result["operations"][2:])
    assert bulk.bus.listeners == []


async def test_two_client_cancellations_do_not_release_inflight_owner_or_listeners(
    bulk: Any,
) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def apply(*arguments: Any, **kwargs: Any) -> None:
        started.set()
        await release.wait()
        await bulk.apply(*arguments, **kwargs)

    bulk.services.async_call.side_effect = apply
    task = asyncio.create_task(
        bulk.service.execute(bulk.rows[:1], parallel=False, timeout=0.1)
    )
    await asyncio.wait_for(started.wait(), 1)
    for _ in range(2):
        task.cancel()
        await asyncio.sleep(0)
    assert not task.done() and len(bulk.bus.listeners) == 1
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert len(bulk.calls) == 1 and bulk.bus.listeners == []


async def test_revocation_after_dispatch_withholds_state_without_claiming_no_attempt(
    bulk: Any,
) -> None:
    async def authorize(_actor: Any) -> None:
        if bulk.calls:
            raise ToolFailure("AUTH_FORBIDDEN", "Actor revoked", status="not_started")

    bulk.service.authorize = authorize
    with pytest.raises(ToolFailure) as caught:
        await bulk.service.execute(bulk.rows[:1], parallel=False, timeout=0.1)
    assert caught.value.status == "outcome_unknown"
    assert caught.value.data["error"]["code"] == "AUTH_FORBIDDEN"
    assert caught.value.data["attempted_operations"] == 1
    assert "transitions" not in caught.value.data and bulk.bus.listeners == []
