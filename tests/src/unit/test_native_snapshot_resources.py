"""Resource restoration keeps Core ownership and does not duplicate recreations."""

import copy
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime.snapshot_payloads import BackupRestoreError
from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_engine_backend import native as native
from .test_native_resource_service import CoreResources
from .test_native_resource_service import resources as resources
from .test_native_snapshot_restore import runtime as runtime


@pytest.fixture
def snapshot_resources(runtime, resources):
    manager, snapshots, store, *_ = runtime
    resource_manager, engine, owner, user, _capture, module = resources
    manager.hass.data = resource_manager.hass.data
    manager.backend.authorize = resource_manager.backend.authorize
    resource_manager.snapshots = snapshots
    saved = {**owner.data["existing"], "url": "/local/saved.js"}
    path = store.capture("dashboard_resource", "existing", saved, "fixture")
    return snapshots, store, path, owner, engine, user, module


async def test_resource_restore_uses_core_with_one_safety_image(
    snapshot_resources, monkeypatch
):
    snapshots, store, path, owner, *_ = snapshot_resources
    before = copy.deepcopy(owner.data["existing"])
    capture = AsyncMock(side_effect=AssertionError("Unexpected automatic recapture"))
    monkeypatch.setattr(snapshots, "resource_backup", capture)
    result = await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert result["apply_status"] == "applied" and owner.calls == ["update"]
    assert owner.data["existing"]["url"] == "/local/saved.js"
    assert store.read_snapshot(result["safety_backup"])["config"] == before
    assert len(store.list_snapshots(domain="dashboard_resource")) == 2
    capture.assert_not_awaited()


async def test_resource_delete_restore_reuses_core_generated_identity_on_repeat(
    snapshot_resources,
):
    snapshots, _store, path, owner, *_ = snapshot_resources
    owner.data.clear()
    result = await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert result["result"]["restore_mode"] == "recreated"
    assert result["result"]["resource"]["id"] == "created"
    repeated = await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert repeated["result"]["restore_mode"] == "already_matches"
    assert repeated["safety_backup"] is None and owner.calls == ["create"]
    data, current = await snapshots.snapshot_comparison(path.name, CallContext("owner"))
    assert data["config"] == current


@pytest.mark.parametrize("change", ["owner", "value"])
async def test_resource_changed_during_safety_io_is_not_overwritten(
    snapshot_resources, monkeypatch, change
):
    snapshots, _store, path, owner, *_ = snapshot_resources
    io = snapshots._io

    async def changed(function, *args):
        result = await io(function, *args)
        if function.__name__ == "capture":
            if change == "owner":
                snapshots.manager.hass.data["lovelace"].resources = CoreResources()
            else:
                owner.data["existing"]["url"] = "/local/concurrent.js"
        return result

    monkeypatch.setattr(snapshots, "_io", changed)
    with pytest.raises(BackupRestoreError) as error:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert error.value.outcome["apply_status"] == "not_applied"
    assert error.value.outcome["safety_backup"] and not owner.calls


async def test_resource_initialization_failure_in_preflight_keeps_unknown_outcome(
    snapshot_resources,
):
    snapshots, _store, path, owner, *_ = snapshot_resources
    owner.loaded = False
    owner.fail_load = True
    with pytest.raises(BackupRestoreError) as error:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert owner.loads == 1 and not owner.calls
    assert error.value.outcome["apply_status"] == "unknown"


async def test_resource_migration_is_not_forgotten_when_safety_io_fails(
    snapshot_resources, monkeypatch
):
    snapshots, _store, path, owner, *_ = snapshot_resources
    owner.loaded = False
    io = snapshots._io

    async def failed(function, *args):
        if function.__name__ == "capture":
            raise OSError("disk unavailable")
        return await io(function, *args)

    monkeypatch.setattr(snapshots, "_io", failed)
    with pytest.raises(BackupRestoreError) as error:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert owner.loads == 1 and not owner.calls
    assert error.value.outcome["apply_status"] == "unknown"


async def test_resource_readonly_diff_does_not_trigger_core_migration(
    snapshot_resources,
):
    snapshots, _store, path, owner, *_ = snapshot_resources
    owner.loaded = False
    with pytest.raises(ToolFailure) as error:
        await snapshots.snapshot_comparison(
            path.name, CallContext("owner", read_only=True)
        )
    assert error.value.status == "not_started" and owner.loads == 0


async def test_resource_recreation_rechecks_the_collection_after_owned_scheduling(
    snapshot_resources, monkeypatch
):
    snapshots, _store, path, owner, _engine, _user, module = snapshot_resources
    owner.data.clear()
    owned = module.await_owned_io

    async def inserted(awaitable):
        if getattr(getattr(awaitable, "cr_code", None), "co_name", None) == "_commit":
            owner.data["another"] = {
                "id": "another",
                "url": "/local/saved.js",
                "type": "module",
            }
        return await owned(awaitable)

    monkeypatch.setattr(module, "await_owned_io", inserted)
    with pytest.raises(BackupRestoreError) as error:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert error.value.outcome["apply_status"] == "not_applied"
    assert not owner.calls and list(owner.data) == ["another"]


async def test_resource_automatic_capture_uses_observed_preimage(snapshot_resources):
    snapshots, store, _path, owner, engine, *_ = snapshot_resources
    before = copy.deepcopy(owner.data["existing"])
    # The fixture's original snapshot shares this key; allow this immediate capture.
    snapshots.manager.policy.flags["auto_backup_throttle_minutes"] = 0
    result = await engine.call(
        "ha_config_delete_dashboard_resource",
        {"resource_id": "existing"},
        CallContext("owner"),
    )
    assert not result.is_error and not owner.data
    rows = store.list_snapshots(domain="dashboard_resource")
    captured = [store.read_snapshot(row["name"]) for row in rows]
    assert any(
        row["config"] == before and row["tool"] == "ha_config_delete_dashboard_resource"
        for row in captured
    )
