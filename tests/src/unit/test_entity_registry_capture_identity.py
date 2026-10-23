"""Explicit legacy registry captures resolve aliases before choosing file identity."""

import copy

from ha_mcp.tools.backup_on_demand import capture_on_demand

from .test_entity_registry_snapshots import registry_history as registry_history


async def test_alias_capture_can_restore_the_same_registration_after_rename(
    registry_history: tuple,
) -> None:
    tools, manager, _, rows, _ = registry_history
    before = copy.deepcopy(rows["sensor.first"])
    path = await capture_on_demand(manager, "entity_registry", "sensor.first")
    saved = manager.read_snapshot(path.name)
    assert saved["entity_id"] == saved["config"]["id"] == "uuid-first"

    await tools.ha_set_entity(
        entity_id="sensor.first", new_entity_id="sensor.renamed", hidden=True
    )
    result = await manager.restore_snapshot(path.name)
    assert result["entity_id"] == "uuid-first"
    assert result["domain"] == "entity_registry"
    assert rows["sensor.first"] == before and "sensor.renamed" not in rows
    assert manager.list_snapshots(domain="entity", entity_id="sensor.first") == []
    assert (
        manager.list_snapshots(domain="entity_registry", entity_id="sensor.first") == []
    )
    assert manager.list_snapshots(domain="entity_registry", entity_id="uuid-first")


async def test_alias_and_registry_uuid_share_the_same_capture_throttle(
    registry_history: tuple,
) -> None:
    _, manager, _, _, _ = registry_history
    manager._settings.auto_backup_throttle_minutes = 60
    first = await manager.maybe_snapshot("entity_registry", "sensor.first")
    second = await manager.maybe_snapshot("entity_registry", "uuid-first")
    assert first is not None and second is None
    assert len(manager.list_snapshots(domain="entity_registry")) == 1


async def test_alias_is_not_rebound_if_renamed_between_resolution_and_capture(
    registry_history: tuple, monkeypatch
) -> None:
    _, manager, _, rows, _ = registry_history
    read = manager._fetch_config_for_snapshot
    requested = []

    async def read_then_rename(handler, identifier, *args, **kwargs):
        requested.append(identifier)
        result = await read(handler, identifier, *args, **kwargs)
        if identifier == "sensor.first":
            renamed = rows.pop("sensor.first")
            renamed["entity_id"] = "sensor.renamed"
            rows["sensor.renamed"] = renamed
            rows["sensor.first"] = {
                **copy.deepcopy(renamed),
                "id": "replacement-uuid",
                "entity_id": "sensor.first",
                "unique_id": "replacement",
            }
        return result

    monkeypatch.setattr(manager, "_fetch_config_for_snapshot", read_then_rename)
    path = await capture_on_demand(manager, "entity_registry", "sensor.first")
    saved = manager.read_snapshot(path.name)
    assert requested == ["sensor.first", "uuid-first"]
    assert saved["entity_id"] == saved["config"]["id"] == "uuid-first"
    assert saved["config"]["entity_id"] == "sensor.renamed"
