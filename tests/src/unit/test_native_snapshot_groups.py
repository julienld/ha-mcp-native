"""Group snapshots use the common coordinator and retain the safety-read owner."""

import asyncio

import pytest

from ha_mcp.runtime.snapshot_payloads import BackupRestoreError
from ha_mcp.tool_api import CallContext

from .test_native_engine_backend import native as native
from .test_native_groups import groups as groups
from .test_native_snapshot_restore import runtime as runtime


@pytest.fixture
def group_history(groups, runtime):
    world, group_manager, _engine, Group = groups
    manager, snapshots, store, *_ = runtime
    group_manager.hass.config = manager.hass.config
    manager.hass = group_manager.hass
    owner = Group("native", name="Current", members=["light.a"], icon="mdi:group")
    world.rows[owner.entity_id] = owner
    saved = {
        "object_id": "native",
        "name": "Saved",
        "entities": ["light.b"],
        "icon": None,
        "all": True,
    }
    path = store.capture("group", "native", saved, "fixture")
    return world, manager, snapshots, store, path, owner, saved, Group


async def test_group_diff_restore_has_one_full_safety_image_and_clears_icon(
    group_history,
):
    world, _, snapshots, store, path, owner, saved, _ = group_history
    _, before = await snapshots.snapshot_comparison(path.name, CallContext("owner"))
    result = await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert result["apply_status"] == "applied" and result["runtime_only"] is True
    assert (
        result["warnings"]
        and store.read_snapshot(result["safety_backup"])["config"] == before
    )
    assert owner.name == saved["name"] and owner.icon is None and owner.mode is all
    assert (
        list(owner.tracking) == saved["entities"] and world.calls.count("create") == 0
    )


@pytest.mark.parametrize("change", ["owner", "value"])
async def test_group_change_during_safety_io_is_refused_before_effect(
    group_history, monkeypatch, change
):
    world, _, snapshots, _, path, owner, _, Group = group_history
    io = snapshots._io

    async def changed(function, *arguments):
        result = await io(function, *arguments)
        if function.__name__ == "capture":
            if change == "owner":
                world.rows[owner.entity_id] = Group("native", name="external")
            else:
                owner.name = "external"
        return result

    monkeypatch.setattr(snapshots, "_io", changed)
    with pytest.raises(BackupRestoreError) as error:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert error.value.outcome["apply_status"] == "not_applied"
    assert error.value.outcome["safety_backup"] and world.calls == []


async def test_deleted_group_recreation_twice_does_not_duplicate_owner(group_history):
    world, _, snapshots, _, path, owner, saved, _ = group_history
    world.rows.pop(owner.entity_id)
    for _ in range(2):
        result = await snapshots.restore_snapshot(path.name, CallContext("owner"))
        assert result["apply_status"] == "applied"
    assert list(world.rows) == ["group.native"] and world.calls.count("create") == 1
    restored = world.rows["group.native"]
    assert restored.name == saved["name"] and restored.mode is all


async def test_legacy_group_snapshot_preserves_unknown_aggregation_mode(group_history):
    _, _, snapshots, store, _, owner, saved, _ = group_history
    legacy = {key: value for key, value in saved.items() if key != "all"}
    owner.mode = all
    path = store.capture("group", "group.native", legacy, "legacy", force=True)
    _, before = await snapshots.snapshot_comparison(path.name, CallContext("owner"))
    assert "all" not in before
    result = await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert owner.mode is all and "older snapshot" in str(result["warnings"])


async def test_group_restore_never_uses_remote_backend_dispatch(group_history):
    _, manager, snapshots, _, path, _, _, _ = group_history

    async def forbidden(*args):
        raise AssertionError("No transport or generic backend operation is needed")

    manager.backend.execute = forbidden
    result = await asyncio.wait_for(
        snapshots.restore_snapshot(path.name, CallContext("owner")), 2
    )
    assert result["apply_status"] == "applied"
