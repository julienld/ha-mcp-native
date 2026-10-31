"""Registry history resolves its UUID and shares one exact safety before-image."""

import asyncio
import copy
import importlib
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime.snapshot_payloads import (
    BackupRestoreError,
    InvalidBackupSnapshotError,
)
from ha_mcp.tool_api import CallContext

from .test_native_entity_registry import _COMPUTED, _Registry
from .test_native_snapshot_restore import runtime as runtime


@pytest.fixture
def entity_history(runtime, monkeypatch):
    manager, snapshots, store, *_ = runtime
    registry = _Registry()
    for entry in registry.rows.values():
        entry.labels = set()
    saved = {**registry.rows["sensor.first"].extended_dict, "name": "Saved"}
    current = registry.rows.pop("sensor.first")
    current.entity_id, current.modified_at = "sensor.renamed", 2.0
    registry.rows[current.entity_id] = current
    er = SimpleNamespace(async_get=lambda hass: registry, COMPUTED_NAME=_COMPUTED)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", er)
    monkeypatch.setattr(sys.modules["homeassistant.helpers"], "entity_registry", er)
    for name in ("label_registry", "area_registry", "category_registry"):
        monkeypatch.setattr(
            sys.modules["homeassistant.helpers"], name, SimpleNamespace(), raising=False
        )
    manager.hass.data = {
        "websocket_api": {"config/entity_registry/update": (None, dict)}
    }
    access = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.registry_access"
    )
    monkeypatch.setattr(access, "json_bytes", lambda value: json.dumps(value).encode())
    owner = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.entity_registry"
    )
    capture = AsyncMock(side_effect=AssertionError("Restore must not recapture"))
    monkeypatch.setattr(owner, "capture_registry_before", capture)
    manager.backend.execute = AsyncMock(
        side_effect=AssertionError("Registry history must use the Core owner")
    )
    path = store.capture("entity_registry", "uuid-first", saved, "fixture")
    return manager, snapshots, store, path, registry, capture


async def test_registry_history_diff_and_restore_follow_uuid_after_entity_rename(
    entity_history,
):
    manager, snapshots, store, path, registry, capture = entity_history
    before = registry.rows["sensor.renamed"].extended_dict
    data, current = await snapshots.snapshot_comparison(path.name, CallContext("owner"))
    assert data["config"]["entity_id"] == "sensor.first" and current == before
    result = await asyncio.wait_for(
        snapshots.restore_snapshot(path.name, CallContext("owner")), 2
    )
    assert result["apply_status"] == "applied"
    assert (
        registry.rows["sensor.first"].name == "Saved"
        and "sensor.renamed" not in registry.rows
    )
    assert registry.rows["sensor.first"].modified_at == 2.0
    assert store.read_snapshot(result["safety_backup"])["config"] == before
    assert len(store.list_snapshots(domain="entity_registry")) == 2
    assert "modified_at" not in registry.calls[0][2]
    capture.assert_not_awaited()
    manager.backend.execute.assert_not_awaited()


async def test_registry_history_conflict_after_safety_capture_never_rebases(
    entity_history, monkeypatch
):
    _, snapshots, _, path, registry, _ = entity_history
    io = snapshots._io

    async def changed(function, *args):
        value = await io(function, *args)
        if function.__name__ == "capture":
            registry.rows["sensor.renamed"].future_core_field = 0
        return value

    monkeypatch.setattr(snapshots, "_io", changed)
    with pytest.raises(BackupRestoreError) as raised:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert raised.value.outcome["apply_status"] == "not_applied"
    assert raised.value.outcome["safety_backup"] and registry.calls == []
    assert type(registry.rows["sensor.renamed"].future_core_field) is int


async def test_deleted_registry_history_explains_why_registration_is_not_recreated(
    entity_history,
):
    _, snapshots, _, path, registry, _ = entity_history
    registry.rows.pop("sensor.renamed")
    with pytest.raises(
        BackupRestoreError, match="cannot recreate the original registry identity"
    ) as raised:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert raised.value.outcome["apply_status"] == "not_applied"
    assert raised.value.outcome["reason"] == "entry_deleted" and registry.calls == []


async def test_registry_snapshot_identity_mismatch_is_refused_before_read_or_write(
    entity_history,
):
    _, snapshots, store, _, registry, _ = entity_history
    row = copy.deepcopy(registry.rows["sensor.renamed"].extended_dict)
    path = store.capture("entity_registry", "different-uuid", row, "fixture")
    with pytest.raises(InvalidBackupSnapshotError, match="does not match"):
        await snapshots.snapshot_comparison(path.name, CallContext("owner"))
    assert registry.calls == []


async def test_registry_capture_callback_accepts_only_matching_domain_source_pairs(
    entity_history,
):
    _, snapshots, store, _, registry, _ = entity_history
    row = registry.rows["sensor.renamed"].extended_dict
    await snapshots.registry_backup(
        {
            "domain": "entity_registry",
            "source_tool": "ha_set_entity",
            "backup_id": row["id"],
            "current_config": row,
        },
        CallContext("owner"),
    )
    assert any(
        store.read_snapshot(item["name"])["tool"] == "ha_set_entity"
        for item in store.list_snapshots(domain="entity_registry")
    )
    with pytest.raises(ValueError, match="domain/source"):
        await snapshots.registry_backup(
            {
                "domain": "entity_registry",
                "source_tool": "ha_set_device",
                "backup_id": row["id"],
                "current_config": row,
            },
            CallContext("owner"),
        )


async def test_registry_restore_keeps_confirmed_phase_details_on_later_failure(
    entity_history, monkeypatch
):
    _, snapshots, _, path, registry, _ = entity_history
    registry.rows["sensor.renamed"].options["sensor"] = {"precision": 9}
    update = registry.async_update_entity_options

    def fail_after_update(*args):
        update(*args)
        raise ValueError("Listener failed after options update")

    monkeypatch.setattr(registry, "async_update_entity_options", fail_after_update)
    with pytest.raises(BackupRestoreError) as raised:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert raised.value.outcome["apply_status"] == "unknown"
    assert raised.value.outcome["confirmed_phases"] == ["registry"]
    assert raised.value.outcome["partial"] is True


async def test_registry_capture_cannot_persist_a_row_under_another_registry_uuid(
    entity_history,
):
    _, snapshots, store, _, registry, _ = entity_history
    row = registry.rows["sensor.renamed"].extended_dict
    before = len(store.list_snapshots(domain="entity_registry"))
    with pytest.raises(ValueError, match="identity"):
        await snapshots.registry_backup(
            {
                "domain": "entity_registry",
                "source_tool": "ha_set_entity",
                "backup_id": "wrong-uuid",
                "current_config": row,
            },
            CallContext("owner"),
        )
    assert len(store.list_snapshots(domain="entity_registry")) == before
