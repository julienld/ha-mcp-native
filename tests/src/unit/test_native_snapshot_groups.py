"""Group snapshots use the common coordinator and retain the safety-read owner."""

import asyncio
from types import SimpleNamespace
from typing import Any

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
        "entities": ["light.a", "light.b"],
        "icon": None,
        "all": True,
    }
    path = store.capture("group", "native", saved, "fixture")
    return world, manager, snapshots, store, path, owner, saved, Group


async def test_group_diff_restore_has_one_full_safety_image_and_clears_icon(
    group_history: Any,
) -> None:
    world, manager, snapshots, store, path, owner, saved, _ = group_history

    async def authorize(actor: CallContext) -> None:
        if world.service_calls:
            await asyncio.sleep(0)
            owner.async_set_context(SimpleNamespace(user_id="member-event"))

    manager.backend.authorize.side_effect = authorize
    _, before = await snapshots.snapshot_comparison(path.name, CallContext("owner"))
    result = await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert result["apply_status"] == "applied" and result["runtime_only"] is True
    assert (
        result["warnings"]
        and store.read_snapshot(result["safety_backup"])["config"] == before
    )
    assert owner.name == saved["name"] and owner.icon is None and owner.mode is all
    assert world.states[owner.entity_id]["state"] == "off"
    assert world.states[owner.entity_id]["context"]["user_id"] == "owner"
    assert world.rows[owner.entity_id] is owner and owner.context.user_id == "owner"
    assert len(world.service_calls) == 1
    assert "icon" not in world.service_calls[0][0]
    assert world.service_calls[0][1].user_id == "owner"
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


async def test_restore_does_not_clear_icon_after_service_replaces_owner(group_history):
    world, _, snapshots, _, path, owner, _, Group = group_history
    replacement = Group("native", name="Other", icon="mdi:account")

    def replace(changed, field):
        if field == "publish":
            world.rows[changed.entity_id] = replacement

    world.callback = replace
    with pytest.raises(BackupRestoreError) as error:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert error.value.outcome["apply_status"] == "unknown"
    assert replacement.icon == "mdi:account" and owner.icon == "mdi:group"
    assert len(world.service_calls) == 1 and "icon" not in world.calls


async def test_restore_keeps_safety_backup_and_icon_when_actor_revoked_before_refresh(
    group_history: Any,
) -> None:
    world, manager, snapshots, store, path, owner, _, _ = group_history

    async def revoke(actor: CallContext) -> None:
        if world.service_calls:
            await asyncio.sleep(0)
            raise PermissionError("Actor revoked after Core accepted the restore")

    manager.backend.authorize.side_effect = revoke
    with pytest.raises(BackupRestoreError) as error:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))

    outcome = error.value.outcome
    assert outcome["apply_status"] == "unknown"
    assert (
        store.read_snapshot(outcome["safety_backup"])["config"]["icon"] == "mdi:group"
    )
    assert owner.icon == "mdi:group" and "icon" not in world.calls
    assert len(world.service_calls) == 1
