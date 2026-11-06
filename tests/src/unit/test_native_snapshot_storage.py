"""Stored snapshot restoration retains Core identities and registry semantics."""

import copy
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from .test_native_snapshot_restore import runtime as runtime


async def test_tag_snapshot_captures_display_name_from_registry_not_stale_storage(
    runtime, monkeypatch
):
    manager, snapshots, _store, _path, _rows, _calls = runtime
    registry = SimpleNamespace(
        async_get=lambda _: SimpleNamespace(name=None, icon="mdi:tag")
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers.entity_registry"],
        "async_get",
        lambda _: registry,
    )
    result = await snapshots._helper_config(
        {
            "helper_type": "tag",
            "entity_id": "tag.saved",
            "current_item": {"id": "stable", "name": "stale"},
        }
    )
    assert result["name"] is None
    assert result["registry_icon"] == "mdi:tag"


async def test_unnamed_tag_restore_clears_registry_name_without_invalid_core_name(
    runtime, monkeypatch
):
    from custom_components.ha_mcp_tools.native_snapshot_domains import StorageBefore
    from ha_mcp.tool_api import CallContext

    manager, snapshots, _store, _path, _rows, _calls = runtime
    row = SimpleNamespace(
        entity_id="tag.live",
        id="row-id",
        platform="tag",
        unique_id="stored",
        name="Current",
        icon="mdi:tag",
    )
    updates = []

    def update(_entity, **changes):
        updates.append(changes)
        for key, value in changes.items():
            setattr(row, key, value)

    registry = SimpleNamespace(
        async_get=lambda _: row,
        async_get_entity_id=lambda *args: row.entity_id,
        async_update_entity=update,
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers.entity_registry"],
        "async_get",
        lambda _: registry,
    )
    original = {"id": "stored", "name": "stale storage"}
    manager.backend.execute = AsyncMock(
        return_value={"success": True, "item": original}
    )
    descriptor = {
        "entity_id": row.entity_id,
        "id": row.id,
        "platform": "tag",
        "unique_id": "stored",
        "values": {"name": "Current", "icon": "mdi:tag"},
    }
    before = StorageBefore({**original, "name": "Current"}, original, descriptor)
    result = await snapshots.domains.apply_storage(
        {
            "domain": "helper_tag",
            "config": {"id": "stored", "name": None, "registry_icon": None},
        },
        before,
        CallContext("owner"),
    )
    sent = manager.backend.execute.call_args.args[1]
    assert "name" not in sent["data"]
    assert sent["expected_item"] == original
    assert sent["expected_registry"] == descriptor
    assert updates == [{"name": None}, {"icon": None}]
    assert result["id"] == "stored"


async def test_missing_zone_is_recreated_once_and_matching_zone_is_left_untouched(
    runtime,
    monkeypatch,
):
    from ha_mcp.tool_api import CallContext

    manager, snapshots, store, _path, _rows, _calls = runtime
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers.entity_registry"],
        "async_get",
        lambda _: SimpleNamespace(async_get_entity_id=lambda *args: None),
    )
    fields = {
        "name": "Park",
        "latitude": 45.0,
        "longitude": -73.0,
        "radius": 100.0,
        "passive": False,
    }
    items = []
    writes = []

    async def execute(op, args, _actor):
        if op == "helpers.storage.read":
            return {"success": False, "error": {"code": "not_found"}}
        if op == "helpers.storage.list":
            return {"items": copy.deepcopy(items)}
        assert op == "helpers.storage.write" and args["action"] == "create"
        writes.append(args)
        item = {"id": "new-id", **args["data"]}
        items.append(item)
        return {"success": True, "item": item}

    manager.backend.execute = execute
    path = store.capture(
        "zone", "removed-id", {"id": "removed-id", **fields}, "fixture"
    )
    first = await snapshots.restore_snapshot(path.name, CallContext("owner"))
    second = await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert first["result"]["restore_mode"] == "recreated"
    assert second["result"]["restore_mode"] == "already_recreated"
    assert len(writes) == 1
    assert second["result"]["warnings"]


async def test_registry_backup_uses_supplied_preimage_and_skips_create(runtime):
    from ha_mcp.tool_api import CallContext

    _manager, snapshots, store, _path, _rows, calls = runtime
    payload = {
        "domain": "category",
        "source_tool": "ha_config_set_category",
        "backup_id": "automation:cat",
        "current_config": {"category_id": "cat", "scope": "automation", "name": "Old"},
    }
    await snapshots.registry_backup(payload, CallContext("owner"))
    await snapshots.registry_backup(
        {**payload, "current_config": None}, CallContext("owner")
    )
    records = store.list_snapshots(domain="category")
    assert len(records) == 1
    assert (
        store.read_snapshot(records[0]["name"])["config"] == payload["current_config"]
    )
    assert not calls
    with pytest.raises(ValueError):
        await snapshots.registry_backup(
            {**payload, "domain": "entity"}, CallContext("owner")
        )


async def test_category_restore_keeps_colon_scope_and_checks_the_same_before_image(
    runtime,
):
    from ha_mcp.tool_api import CallContext

    manager, snapshots, store, _path, _rows, _calls = runtime
    before = {"category_id": "stable", "name": "Current", "icon": None}
    saved = {
        "scope": "custom:scope",
        "category_id": "stable",
        "name": "Saved",
        "icon": "mdi:tag",
    }
    sent = []

    async def execute(operation, args, actor):
        sent.append((operation, args))
        if operation == "registry.category.read":
            assert args == {"scope": "custom:scope", "category_id": "stable"}
            return before
        assert operation == "registry.category.update"
        assert args["expected_item"] == before
        assert args["data"] == saved
        return {
            "success": True,
            "result": {k: v for k, v in saved.items() if k != "scope"},
        }

    manager.backend.execute = execute
    path = store.capture("category", "custom:scope:stable", saved, "fixture")
    result = await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert result["apply_status"] == "applied"
    safety = store.read_snapshot(result["safety_backup"])
    assert safety["config"] == {"scope": "custom:scope", **before}
    assert len(sent) == 2


async def test_native_area_restore_preserves_floor_assignment_in_core_payload(runtime):
    from ha_mcp.tool_api import CallContext

    manager, snapshots, store, _path, _rows, _calls = runtime
    before = {"area_id": "room", "name": "Current", "floor_id": "wrong"}
    saved = {"kind": "area", "area_id": "room", "name": "Saved", "floor_id": "recorded"}

    async def execute(operation, args, actor):
        if operation == "registry.area.read":
            assert args == {"area_id": "room"}
            return before
        assert operation == "registry.area.update"
        assert args == {
            "data": {k: v for k, v in saved.items() if k != "kind"},
            "expected_item": before,
        }
        return {"success": True, "result": args["data"]}

    manager.backend.execute = execute
    path = store.capture("area_or_floor", "area:room", saved, "fixture")
    result = await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert result["result"]["floor_id"] == "recorded"
    assert store.read_snapshot(result["safety_backup"])["config"] == {
        "kind": "area",
        **before,
    }


async def test_area_restore_waits_for_the_shared_writer_lock_before_reading_safety(
    runtime, monkeypatch
):
    import asyncio
    from contextlib import asynccontextmanager

    from ha_mcp.tool_api import CallContext
    from ha_mcp.utils import registry_update_lock as locks

    manager, snapshots, store, _path, _rows, _calls = runtime
    current = {"area_id": "room", "name": "Before writer", "floor_id": None}
    read = []

    async def execute(operation, args, actor):
        if operation == "registry.area.read":
            read.append(copy.deepcopy(current))
            return copy.deepcopy(current)
        assert args["expected_item"] == current
        return {"success": True, "result": args["data"]}

    manager.backend.execute = execute
    original = locks.registry_update_lock
    reached = asyncio.Event()

    @asynccontextmanager
    async def observed(kind, identifier):
        if (kind, identifier) == ("area", "room"):
            reached.set()
        async with original(kind, identifier):
            yield

    monkeypatch.setattr(locks, "registry_update_lock", observed)
    saved = {"kind": "area", "area_id": "room", "name": "Restored", "floor_id": None}
    path = store.capture("area_or_floor", "area:room", saved, "fixture")
    task = None
    try:
        async with original("area", "room"):
            task = asyncio.create_task(
                snapshots.restore_snapshot(path.name, CallContext("owner"))
            )
            await asyncio.wait_for(reached.wait(), 2)
            assert not read, "A queued restore must not capture an obsolete preimage"
            current["name"] = "Completed writer"
        result = await task
        assert (
            store.read_snapshot(result["safety_backup"])["config"]["name"]
            == "Completed writer"
        )
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
