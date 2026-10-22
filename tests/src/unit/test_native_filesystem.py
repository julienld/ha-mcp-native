"""Native filesystem calls keep the explicit entry, backups and live actor gates."""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_engine_backend import native as native
from .test_native_file_access import files as files


@pytest.fixture
def filesystem(files, tmp_path, monkeypatch):
    module, owner, target = files
    from custom_components.ha_mcp_tools.native_operations.filesystem import (
        NativeFilesystem,
    )
    from custom_components.ha_mcp_tools.native_server_policy import NativePolicy

    for name in (
        "ENABLE_AUTO_BACKUP",
        "HAMCP_ENABLE_FILESYSTEM_TOOLS",
        "HAMCP_BACKUP_DIR",
        "ENABLE_BETA_FEATURES",
    ):
        monkeypatch.delenv(name, raising=False)
    from custom_components.ha_mcp_tools.native_snapshots import NativeSnapshots

    owner.entry = SimpleNamespace(entry_id="file-entry", data={}, disabled_by=None)
    owner.hass.config_entries = SimpleNamespace(async_get_entry=lambda _id: owner.entry)
    manager = SimpleNamespace(
        hass=owner.hass,
        policy=NativePolicy(
            flags={
                "enable_filesystem_tools": True,
                "enable_beta_features": True,
                "enable_auto_backup": True,
                "auto_backup_dir": str(tmp_path / "history"),
            }
        ),
        backend=SimpleNamespace(authorize=AsyncMock()),
        settings_ui=SimpleNamespace(_io=owner.hass.async_add_executor_job),
    )
    manager.snapshots = NativeSnapshots(manager)
    service = NativeFilesystem(manager, CallContext("owner"))
    return service, manager, owner, target


async def test_native_write_captures_exact_text_before_any_change(filesystem):
    service, manager, _owner, target = filesystem
    target.write_bytes(b"# untouched\r\nbefore\r\n")
    result = await service.write_file("www/notes.txt", "after", overwrite=True)
    assert result["success"] and target.read_text() == "after"
    store = manager.snapshots._store()
    rows = store.list_snapshots(domain="file")
    assert len(rows) == 1
    assert store.read_snapshot(rows[0]["name"])["config"] == "# untouched\r\nbefore\r\n"


@pytest.mark.parametrize(
    "change", ["entry", "backup", "permissions", "target", "actor"]
)
async def test_native_write_revalidates_after_awaited_capture(filesystem, change):
    service, manager, owner, target = filesystem
    if change == "permissions":
        target.parent.rename(target.parent.with_name("extra"))
        target = target.parent.with_name("extra") / target.name
        owner.hass.data["ha_mcp_tools"]["allowed_paths"] = ["extra"]
    capture = manager.snapshots.file_backup
    captured = []

    async def after_capture(*args):
        await capture(*args)
        captured.append(True)
        if change == "entry":
            owner.hass.data["ha_mcp_tools"].pop("file_access_owner")
        elif change == "backup":
            manager.policy.flags["enable_auto_backup"] = False
        elif change == "permissions":
            owner.hass.data["ha_mcp_tools"]["allowed_paths"] = []
        elif change == "target":
            target.write_text("external")
        else:
            manager.backend.authorize.side_effect = PermissionError("revoked")

    manager.snapshots.file_backup = after_capture
    path = f"{target.parent.name}/{target.name}"
    with pytest.raises(ToolFailure) as raised:
        await service.write_file(path, "requested", overwrite=True)
    assert raised.value.status == "not_started" and captured == [True]
    assert target.read_text() == ("external" if change == "target" else "before")


async def test_missing_entry_and_backup_failure_never_dispatch_file_write(
    filesystem, monkeypatch
):
    service, manager, owner, target = filesystem
    owner.entry = None
    with pytest.raises(ToolFailure, match="File & YAML Tools"):
        await service.write_file("www/notes.txt", "after", overwrite=True)
    assert target.read_text() == "before"
    owner.entry = SimpleNamespace(entry_id="entry", data={}, disabled_by=None)
    store = manager.snapshots._store()
    monkeypatch.setattr(
        store, "capture", lambda *_args: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(ToolFailure) as raised:
        await service.delete_file("www/notes.txt", confirm=True)
    assert (
        raised.value.code == "BACKUP_CAPTURE_FAILED" and target.read_text() == "before"
    )


async def test_file_lock_keeps_second_snapshot_after_first_write(filesystem):
    service, manager, _owner, target = filesystem
    entered, release = asyncio.Event(), asyncio.Event()
    capture = manager.snapshots.file_backup
    images = []

    async def wait_first(payload, actor):
        images.append(payload["current_config"])
        if len(images) == 1:
            entered.set()
            await release.wait()
        await capture(payload, actor)

    manager.snapshots.file_backup = wait_first
    first = asyncio.create_task(
        service.write_file("www/notes.txt", "first", overwrite=True)
    )
    await entered.wait()
    second = asyncio.create_task(
        service.write_file("www/notes.txt", "second", overwrite=True)
    )
    await asyncio.sleep(0)
    assert images == ["before"]
    release.set()
    await asyncio.gather(first, second)
    assert images == ["before", "first"] and target.read_text() == "second"


def test_file_projection_matches_the_frozen_public_contract(filesystem):
    _service, manager, *_ = filesystem
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_filesystem_tools"
    )
    import json
    from pathlib import Path

    from .test_native_catalog_contract import _changed_inputs

    baseline = json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "docs/native-migration/catalog-baseline.json"
        ).read_text()
    )["tools"]
    by_name = {row["name"]: row for row in baseline}
    definitions = module.filesystem_definitions(manager)
    from custom_components.ha_mcp_tools.native_yaml_tools import yaml_definitions

    definitions += yaml_definitions(manager)
    for definition in definitions:
        expected = by_name[definition.name]
        assert not _changed_inputs(expected["inputSchema"], definition.input_schema)
        assert definition.annotations == expected["annotations"]
        assert definition.description == expected["description"]


@pytest.mark.parametrize("confirm", [None, False, True])
async def test_delete_file_runs_the_public_facade_and_requires_confirmation(
    filesystem, confirm
):
    from custom_components.ha_mcp_tools.native_filesystem_tools import (
        filesystem_definitions,
    )
    from ha_mcp.runtime import Engine

    _service, manager, _owner, target = filesystem
    engine = Engine(manager.backend, filesystem_definitions(manager))
    arguments = {"path": "www/notes.txt"}
    if confirm is not None:
        arguments["confirm"] = confirm
    result = await engine.call("ha_delete_file", arguments, CallContext("owner"))
    rows = manager.snapshots._store().list_snapshots(domain="file")
    if confirm:
        assert not result.is_error and result.data["success"]
        assert not target.exists() and len(rows) == 1
        assert (
            manager.snapshots._store().read_snapshot(rows[0]["name"])["config"]
            == "before"
        )
    else:
        assert result.is_error and result.status == "not_started"
        assert result.data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert target.read_text() == "before" and rows == []
