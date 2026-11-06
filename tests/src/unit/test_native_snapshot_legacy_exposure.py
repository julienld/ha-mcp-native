"""Exposure history restores the absence marker through its actual Core owner."""

import asyncio
import importlib
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime.snapshot_payloads import BackupRestoreError
from ha_mcp.tool_api import CallContext

from .test_native_legacy_exposure import _ExposedEntity, _Owner
from .test_native_snapshot_restore import runtime as runtime


@pytest.fixture
def exposure_history(runtime, monkeypatch):
    manager, snapshots, store, *_ = runtime
    owner = _Owner()
    owner.entities["sensor.legacy"] = _ExposedEntity(
        {"conversation": {"should_expose": False}}
    )
    registry_rows = {}
    er = SimpleNamespace(
        async_get=lambda hass: SimpleNamespace(async_get=registry_rows.get)
    )
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", er)
    monkeypatch.setattr(sys.modules["homeassistant.helpers"], "entity_registry", er)
    module = SimpleNamespace(
        ExposedEntity=_ExposedEntity,
        ExposedEntities=_Owner,
        DATA_EXPOSED_ENTITIES="exposed",
    )
    monkeypatch.setitem(
        sys.modules, "homeassistant.components.homeassistant.exposed_entities", module
    )
    parent = sys.modules.get(
        "homeassistant.components.homeassistant", SimpleNamespace()
    )
    monkeypatch.setitem(sys.modules, "homeassistant.components.homeassistant", parent)
    monkeypatch.setattr(parent, "exposed_entities", module, raising=False)
    manager.hass.data = {"exposed": owner}
    access = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.registry_access"
    )
    monkeypatch.setattr(access, "json_bytes", lambda value: json.dumps(value).encode())
    operations = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.legacy_exposure"
    )
    capture = AsyncMock(side_effect=AssertionError("Restore must not recapture"))
    monkeypatch.setattr(operations, "capture_registry_before", capture)
    path = store.capture(
        "entity_exposure",
        "sensor.legacy",
        {"entity_id": "sensor.legacy", "preferences": None},
        "fixture",
    )
    return snapshots, store, path, owner, registry_rows, capture


async def test_exposure_history_saves_explicit_false_then_restores_true_absence(
    exposure_history,
):
    snapshots, store, path, owner, _, capture = exposure_history
    _, before = await snapshots.snapshot_comparison(path.name, CallContext("owner"))
    assert before["preferences"]["assistants"]["conversation"]["should_expose"] is False
    result = await asyncio.wait_for(
        snapshots.restore_snapshot(path.name, CallContext("owner")), 2
    )
    assert result["apply_status"] == "applied" and "sensor.legacy" not in owner.entities
    assert store.read_snapshot(result["safety_backup"])["config"] == before
    assert len(store.list_snapshots(domain="entity_exposure")) == 2 and owner.saves == 1
    capture.assert_not_awaited()


async def test_exposure_history_refuses_a_new_registration_during_safety_io(
    exposure_history, monkeypatch
):
    snapshots, _, path, owner, registry, _ = exposure_history
    io = snapshots._io

    async def changed(function, *args):
        result = await io(function, *args)
        if function.__name__ == "capture":
            registry["sensor.legacy"] = SimpleNamespace(id="new-registration")
        return result

    monkeypatch.setattr(snapshots, "_io", changed)
    with pytest.raises(BackupRestoreError) as raised:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert raised.value.outcome["apply_status"] == "not_applied"
    assert "sensor.legacy" in owner.entities and owner.saves == 0


async def test_exposure_history_listener_failure_keeps_safety_and_unknown_outcome(
    exposure_history,
):
    snapshots, _, path, owner, _, _ = exposure_history

    def fail():
        raise ValueError("Listener failed")

    owner._listeners["conversation"] = [fail]
    with pytest.raises(BackupRestoreError) as raised:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert (
        raised.value.outcome["apply_status"] == "unknown"
        and raised.value.outcome["safety_backup"]
    )
    assert "sensor.legacy" not in owner.entities and owner.saves == 1
