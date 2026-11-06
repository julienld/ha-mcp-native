"""Component registry history exercises the actual NativeEntityRegistry owner.

The retired component transport scenarios are mapped to existing canonical
coverage in docs/native-migration/entity-backup-test-migration.json.
"""

import asyncio
import copy

import pytest

from ha_mcp.runtime.snapshot_payloads import BackupRestoreError
from ha_mcp.tool_api import CallContext

from .test_native_snapshot_entity_registry import entity_history as entity_history
from .test_native_snapshot_restore import runtime as runtime


@pytest.mark.parametrize("change", ["rename", "replacement"])
async def test_native_registry_restore_refuses_identity_changes_after_safety_capture(
    entity_history, monkeypatch, change
):
    _, snapshots, _, path, registry, _ = entity_history
    io = snapshots._io

    async def capture_then_change(function, *args):
        result = await io(function, *args)
        if function.__name__ == "capture":
            row = registry.rows.pop("sensor.renamed")
            if change == "rename":
                row.entity_id = "sensor.third"
            else:
                row.id, row.unique_id, row.created_at = (
                    "replacement",
                    "replacement",
                    2.0,
                )
            registry.rows[row.entity_id] = row
        return result

    monkeypatch.setattr(snapshots, "_io", capture_then_change)
    with pytest.raises(BackupRestoreError) as raised:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert raised.value.outcome["apply_status"] == "not_applied"
    assert registry.calls == []
    assert any(row.name == "Before" for row in registry.rows.values())


@pytest.mark.parametrize(
    "error", [RuntimeError("Listener failed"), asyncio.CancelledError()]
)
async def test_native_registry_failure_after_first_write_never_replays_or_loses_safety(
    entity_history, monkeypatch, error
):
    _, snapshots, store, path, registry, _ = entity_history
    before = copy.deepcopy(registry.rows["sensor.renamed"].extended_dict)
    update = registry.async_update_entity

    def update_then_fail(*args, **kwargs):
        update(*args, **kwargs)
        raise error

    monkeypatch.setattr(registry, "async_update_entity", update_then_fail)
    with pytest.raises((BackupRestoreError, asyncio.CancelledError)) as raised:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    if isinstance(raised.value, BackupRestoreError):
        assert raised.value.outcome["apply_status"] == "unknown"
    assert len(registry.calls) == 1
    assert registry.calls[0][0] == "update"
    assert registry.rows["sensor.first"].name == "Saved"
    safety = [
        store.read_snapshot(item["name"])
        for item in store.list_snapshots(domain="entity", entity_id="sensor.renamed")
    ]
    assert len(safety) == 1 and safety[0]["config"] == before
    assert safety[0]["config"]["id"] == "uuid-first"
    assert store.read_snapshot(path.name)["domain"] == "entity_registry"
