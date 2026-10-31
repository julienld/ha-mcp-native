"""Installed blueprint text uses the shared store and Core restore coordinator."""

import asyncio

import pytest
import yaml

from ha_mcp.runtime.snapshot_payloads import (
    BackupRestoreError,
    InvalidBackupSnapshotError,
)
from ha_mcp.tool_api import CallContext

from .test_native_blueprint_management import blueprints as blueprints
from .test_native_blueprint_management import source
from .test_native_engine_backend import native as native
from .test_native_snapshot_restore import runtime as runtime


@pytest.fixture
def history(runtime, blueprints):
    manager, snapshots, store, *_ = runtime
    service, owner, _backup, active = blueprints
    owner.hass.config = manager.hass.config
    owner.hass.async_add_executor_job = manager.hass.async_add_executor_job
    manager.hass = owner.hass
    manager.backend.authorize = service.authorize
    service.backup = snapshots.blueprint_backup
    return service, owner, active, snapshots, store


@pytest.mark.parametrize("domain", ["automation", "script"])
async def test_blueprint_history_preserves_text_and_restores_via_Core_with_one_safety_image(
    history, domain
):
    service, owner, _active, snapshots, store = history
    owner.domain = domain
    owner.hass.data["blueprint"] = {domain: owner}
    target = owner.blueprint_folder / "saved.yaml"
    initial = "# local comment\r\n" + source("Saved", domain=domain).replace(
        "\n", "\r\n"
    )
    target.write_bytes(initial.encode())
    await service.manage(
        "save",
        domain=domain,
        path="saved",
        yaml=source("Changed", domain=domain),
        overwrite=True,
    )
    files = store.list_snapshots(domain=f"blueprint_{domain}")
    assert len(files) == 1
    name = files[0]["name"]
    data, before = await snapshots.snapshot_comparison(name, CallContext("owner"))
    assert data["config"] == initial and before == target.read_bytes().decode()
    restored = await asyncio.wait_for(
        snapshots.restore_snapshot(name, CallContext("owner")), 3
    )
    assert restored["apply_status"] == "applied" and restored["safety_backup"]
    assert store.read_snapshot(restored["safety_backup"])["config"] == before
    assert len(store.list_snapshots(domain=f"blueprint_{domain}")) == 2
    assert yaml.safe_load(target.read_text()) == yaml.safe_load(initial)
    # Core's original writer reserializes, like the old blueprint/save restore.
    assert "local comment" not in target.read_text()


@pytest.mark.parametrize("change", ["file", "revoke"])
async def test_blueprint_restore_rechecks_after_safety_capture(
    history, monkeypatch, change
):
    _service, owner, active, snapshots, store = history
    target = owner.blueprint_folder / "saved.yaml"
    target.write_text(source("Current"))
    path = store.capture(
        "blueprint_automation", "saved.yaml", source("Saved"), "fixture"
    )
    io = snapshots._io

    async def during_safety(function, *args):
        result = await io(function, *args)
        if function.__name__ == "capture":
            if change == "file":
                target.write_text(source("External"))
            else:
                active[0] = False
        return result

    monkeypatch.setattr(snapshots, "_io", during_safety)
    with pytest.raises(BackupRestoreError) as raised:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert raised.value.outcome["apply_status"] == "not_applied" and not owner.calls


async def test_deleted_blueprint_restores_from_its_installed_copy_without_refetch(
    history,
):
    service, owner, _active, snapshots, store = history
    target = owner.blueprint_folder / "deleted.yaml"
    original = source("Installed")
    target.write_text(original)
    await service.manage("delete", path="deleted.yaml", confirm=True)
    assert not target.exists()
    snapshot = store.list_snapshots(domain="blueprint_automation")[0]["name"]
    restored = await snapshots.restore_snapshot(snapshot, CallContext("owner"))
    assert restored["apply_status"] == "applied" and restored["safety_backup"] is None
    assert yaml.safe_load(target.read_text()) == yaml.safe_load(original)
    assert owner.calls == [("delete", "deleted.yaml"), ("save", "deleted.yaml", True)]


async def test_blueprint_restore_rejects_non_text_snapshot_before_any_Core_callback(
    history,
):
    _service, owner, _active, snapshots, store = history
    path = store.capture(
        "blueprint_automation", "invalid.yaml", {"not": "yaml text"}, "fixture"
    )
    with pytest.raises(InvalidBackupSnapshotError, match="YAML text"):
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert not owner.calls
