"""Public native bulk calls resolve Core targets and retain owned operation IDs."""

import asyncio
import importlib
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.runtime.bulk_selection import SelectorTopology
from ha_mcp.tool_api import CallContext

from .test_component_ws_search import FakeState
from .test_native_bulk_dispatch import bulk as bulk
from .test_native_engine_backend import native as native


@pytest.fixture
def bulk_tools(bulk: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    module = importlib.import_module("custom_components.ha_mcp_tools.native_bulk_tools")
    owners = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.bulk_control"
    )
    store = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.device_operations"
    )
    cv = SimpleNamespace(
        entity_ids=lambda values: [value.strip().lower() for value in values]
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"], "config_validation", cv, raising=False
    )
    registry_rows = {name: SimpleNamespace(hidden_by=None) for name in bulk.states}
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"].entity_registry,
        "async_get",
        lambda _hass: SimpleNamespace(async_get=registry_rows.get),
    )
    backend = SimpleNamespace(
        authorize=AsyncMock(), _check_service_policy=lambda *_: None
    )
    manager = SimpleNamespace(
        hass=bulk.hass,
        backend=backend,
        visibility=SimpleNamespace(_config=AsyncMock(return_value=None)),
        device_operations=store.NativeDeviceOperations(),
    )

    async def executor(function: Any, *args: Any) -> Any:
        return await asyncio.to_thread(function, *args)

    manager.hass.async_add_executor_job = executor

    def topology(_self: Any, **_kwargs: Any) -> SelectorTopology:
        return SelectorTopology(
            [state.as_dict() for state in bulk.states.values()],
            [{"entity_id": name, "area_id": "room"} for name in bulk.states],
            [],
            [{"area_id": "room", "floor_id": "floor"}],
            [{"floor_id": "floor"}],
        )

    monkeypatch.setattr(owners.NativeBulkControl, "_topology", topology)
    return SimpleNamespace(
        world=bulk,
        manager=manager,
        engine=Engine(backend, module.bulk_definitions(manager)),
        owners=owners,
        registry_rows=registry_rows,
    )


async def test_native_bulk_public_call_and_status_use_the_same_core_outcome(
    bulk_tools: Any,
) -> None:
    result = await bulk_tools.engine.call(
        "ha_bulk_control",
        {
            "operations": [
                {"entity_id": "light.item_0", "action": "on"},
                {"entity_id": "light.item_1", "action": "on"},
            ]
        },
        CallContext("actor", client_id="client"),
    )
    assert not result.is_error, result.data
    assert result.data["successful_commands"] == 2
    ids = result.data["operation_ids"]
    assert len(ids) == 2 and all(identifier.startswith("device-") for identifier in ids)
    status = await bulk_tools.engine.call(
        "ha_get_operation_status",
        {"operation_id": ids, "timeout_seconds": 0},
        CallContext("actor", client_id="client", read_only=True),
    )
    assert not status.is_error and status.data["completed"] == 2, status.data
    assert len(bulk_tools.world.calls) == 2 and not bulk_tools.world.bus.listeners


async def test_native_bulk_selector_preview_is_read_only_and_shared_resolution(
    bulk_tools: Any,
) -> None:
    result = await bulk_tools.engine.call(
        "ha_bulk_control",
        {
            "selector": {
                "domain": "light",
                "area_ids": ["room"],
                "exclude_entity_ids": ["light.item_0"],
            },
            "action": "on",
            "dry_run": True,
        },
        CallContext("actor", read_only=True),
    )
    assert not result.is_error and result.data["dry_run"], result.data
    assert len(result.data["resolution"]["resolved_entity_ids"]) == 7
    assert "light.item_0" not in result.data["resolution"]["resolved_entity_ids"]
    assert (
        not bulk_tools.world.calls and not bulk_tools.manager.device_operations.entries
    )


async def test_native_bulk_invalid_action_preserves_successful_sibling(
    bulk_tools: Any,
) -> None:
    result = await bulk_tools.engine.call(
        "ha_bulk_control",
        {
            "operations": [
                {"entity_id": "light.item_0", "action": "invalid"},
                {"entity_id": "light.item_1", "action": "on"},
            ]
        },
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert result.data["successful_commands"] == result.data["failed_commands"] == 1
    assert result.data["results"][0]["error"]["code"] == "SERVICE_INVALID_ACTION"
    assert len(bulk_tools.world.calls) == 1


async def test_missing_core_service_is_a_row_failure_without_dropping_valid_siblings(
    bulk_tools: Any,
) -> None:
    bulk_tools.world.services.has_service = lambda domain, service: service == "turn_on"
    result = await bulk_tools.engine.call(
        "ha_bulk_control",
        {
            "operations": [
                {"entity_id": "light.item_0", "action": "off"},
                {"entity_id": "light.item_1", "action": "on"},
            ]
        },
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert result.data["successful_commands"] == result.data["failed_commands"] == 1
    assert result.data["results"][0]["error"]["code"] == "SERVICE_NOT_FOUND"
    assert len(bulk_tools.world.calls) == 1


async def test_native_bulk_unknown_provider_is_partial_without_replay(
    bulk_tools: Any,
) -> None:
    async def fail(*args: Any, **kwargs: Any) -> None:
        await bulk_tools.world.apply(*args, **kwargs)
        raise RuntimeError("effect followed by provider error")

    bulk_tools.world.services.async_call.side_effect = fail
    actor = CallContext("actor", client_id="client")
    result = await bulk_tools.engine.call(
        "ha_bulk_control",
        {
            "parallel": False,
            "operations": [
                {"entity_id": "light.item_0", "action": "on"},
                {"entity_id": "light.item_1", "action": "on"},
            ],
        },
        actor,
    )
    assert not result.is_error and result.data["partial"], result.data
    assert result.status == "outcome_unknown"
    assert result.data["results"][0]["error"]["code"] == "OUTCOME_UNKNOWN"
    assert (
        not result.data["results"][1]["attempted"] and len(bulk_tools.world.calls) == 1
    )
    status = await bulk_tools.engine.call(
        "ha_get_operation_status",
        {"operation_id": result.data["operation_ids"], "timeout_seconds": 0},
        actor,
    )
    assert not status.is_error and status.data["failed"] == 2, status.data
    assert status.data["detailed_results"][0]["operation_outcome"] == "outcome_unknown"


async def test_native_bulk_visibility_is_checked_again_after_resolution(
    bulk_tools: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = 0

    async def hidden(_self: Any) -> set[str]:
        nonlocal seen
        seen += 1
        return set() if seen == 1 else {"light.item_0"}

    monkeypatch.setattr(bulk_tools.owners.NativeBulkControl, "_hidden", hidden)
    result = await bulk_tools.engine.call(
        "ha_bulk_control",
        {"selector": {"domain": "light", "area_ids": ["room"]}, "action": "on"},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "not_started", result.data
    assert result.data["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"
    assert not bulk_tools.world.calls


async def test_repeated_toggle_targets_have_separate_preimages_and_confirmations(
    bulk_tools: Any,
) -> None:
    world = bulk_tools.world

    async def toggle(domain: str, service: str, data: dict, **kwargs: Any) -> None:
        world.calls.append((domain, service, data, kwargs))
        before = world.states[data["entity_id"]]
        state = FakeState(data["entity_id"], "on" if before.state == "off" else "off")
        world.states[state.entity_id] = state
        world.bus.fire(state, kwargs["context"])

    world.services.async_call.side_effect = toggle
    result = await bulk_tools.engine.call(
        "ha_bulk_control",
        {"operations": [{"entity_id": "light.item_0", "action": "toggle"}] * 2},
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    first, second = result.data["results"]
    assert first["final_state"] == "on" and second["final_state"] == "off"
    assert second["transitions"][0]["old_state"]["state"] == "on"
    assert result.data["execution_mode"] == "sequential" and result.data["warnings"]
    assert len(world.calls) == 2 and not world.bus.listeners


async def test_selector_registry_hidden_change_before_dispatch_prevents_the_write(
    bulk_tools: Any,
) -> None:
    calls = 0

    async def config(_name: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            bulk_tools.registry_rows["light.item_0"].hidden_by = "user"

    bulk_tools.manager.visibility._config.side_effect = config
    result = await bulk_tools.engine.call(
        "ha_bulk_control",
        {"selector": {"domain": "light", "area_ids": ["room"]}, "action": "on"},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "not_started", result.data
    assert result.data["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"
    assert not bulk_tools.world.calls


async def test_group_membership_change_during_admission_cannot_expand_the_batch(
    bulk_tools: Any,
) -> None:
    group = FakeState("light.group", "off")
    group.attributes = {"entity_id": ["light.item_0"]}
    bulk_tools.world.states[group.entity_id] = group
    calls = 0

    async def config(_name: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            group.attributes["entity_id"].append("light.item_1")

    bulk_tools.manager.visibility._config.side_effect = config
    result = await bulk_tools.engine.call(
        "ha_bulk_control",
        {"operations": [{"entity_id": "light.group", "action": "on"}]},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "not_started", result.data
    assert result.data["error"]["code"] == "CONFIG_CONFLICT"
    assert not bulk_tools.world.calls
