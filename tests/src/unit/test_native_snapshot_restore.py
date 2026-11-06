"""Restore selects stable identities and keeps its safety file owned until completion."""

import asyncio
import copy
import json
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ._embedded_stubs import install

install()


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from custom_components.ha_mcp_tools.native_server_policy import NativePolicy
    from custom_components.ha_mcp_tools.native_settings import NativeSettings
    from custom_components.ha_mcp_tools.native_snapshots import NativeSnapshots

    for key in (
        "ENABLE_AUTO_BACKUP",
        "BACKUP_READ_ONLY",
        "HAMCP_BACKUP_DIR",
        "ENABLE_SNAPSHOT_ACTIONS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.helpers.json",
        SimpleNamespace(json_bytes=lambda value: json.dumps(value).encode()),
    )
    rows = {
        "original": {"id": "original", "name": "Current original"},
        "other": {"id": "other", "name": "Unrelated"},
    }
    aliases = {"input_boolean.renamed": "original", "input_boolean.old": "other"}
    calls = []

    async def execute(operation, arguments, actor):
        calls.append((operation, copy.deepcopy(arguments)))
        if operation == "helpers.storage.read":
            target = arguments.get("item_id") or aliases.get(arguments.get("entity_id"))
            if target not in rows:
                return {"success": False, "error": {"code": "not_found"}}
            return {
                "success": True,
                "item": copy.deepcopy(rows[target]),
                "entity_id": "input_boolean.renamed",
            }
        if operation == "helpers.storage.write":
            target = arguments["item_id"]
            assert arguments["expected_item"] == rows[target]
            rows[target].update(arguments["data"])
            return {"success": True, "item": copy.deepcopy(rows[target])}
        raise AssertionError(operation)

    registry = SimpleNamespace(async_get=lambda _entity: SimpleNamespace(icon=None))
    er = SimpleNamespace(async_get=lambda _hass: registry)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", er)
    monkeypatch.setattr(sys.modules["homeassistant.helpers"], "entity_registry", er)
    manager = SimpleNamespace(
        hass=SimpleNamespace(
            config=SimpleNamespace(path=lambda _subdir: str(tmp_path)),
            async_add_executor_job=lambda function, *args: asyncio.ensure_future(
                asyncio.to_thread(function, *args)
            ),
        ),
        backend=SimpleNamespace(authorize=AsyncMock(), execute=execute),
        policy=NativePolicy(
            flags={
                "enable_auto_backup": True,
                "backup_read_only": False,
                "enable_snapshot_actions": True,
            }
        ),
    )
    manager.settings_ui = NativeSettings(manager)
    snapshots = NativeSnapshots(manager)
    manager.snapshots = snapshots
    store = snapshots._store()
    path = store.capture(
        "helper_input_boolean",
        "input_boolean.old",
        {"id": "original", "name": "Snapshot"},
        "fixture",
    )
    return manager, snapshots, store, path, rows, calls


async def test_edit_restore_does_not_require_full_ha_snapshot_flag(runtime):
    from ha_mcp.tool_api import CallContext

    manager, snapshots, store, path, rows, _calls = runtime
    manager.policy.flags["enable_snapshot_actions"] = False
    result = await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert result["apply_status"] == "applied"
    assert rows["original"]["name"] == "Snapshot"
    assert rows["other"]["name"] == "Unrelated"
    assert store.read_snapshot(result["safety_backup"])["config"]["id"] == "original"


async def test_diff_follows_stored_id_after_an_entity_alias_is_reassigned(runtime):
    from ha_mcp.tool_api import CallContext

    _, snapshots, _, path, _rows, calls = runtime
    _data, current = await snapshots.snapshot_comparison(
        path.name, CallContext("owner")
    )
    assert current["id"] == "original"
    assert calls[-1][1]["item_id"] == "original"


async def test_restore_keeps_one_store_when_backup_directory_changes(
    runtime, tmp_path, monkeypatch
):
    from ha_mcp.runtime.snapshot_store import SnapshotStore
    from ha_mcp.tool_api import CallContext

    _, snapshots, store, path, _rows, _calls = runtime
    other = SnapshotStore(tmp_path / "other")
    stores = iter([store, other])
    monkeypatch.setattr(snapshots, "_store", lambda: next(stores))
    result = await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert (store.directory / result["safety_backup"]).is_file()
    assert not (other.directory / result["safety_backup"]).exists()


async def test_cancellation_does_not_release_source_pin_while_safety_io_runs(
    runtime, monkeypatch
):
    from ha_mcp.runtime.snapshot_files import SnapshotInUseError
    from ha_mcp.tool_api import CallContext

    _, snapshots, store, path, _rows, _calls = runtime
    started, release = threading.Event(), threading.Event()
    capture = store.capture

    def delayed(*args, **kwargs):
        started.set()
        release.wait(5)
        return capture(*args, **kwargs)

    monkeypatch.setattr(store, "capture", delayed)
    task = asyncio.create_task(
        snapshots.restore_snapshot(path.name, CallContext("owner"))
    )
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        for _ in range(3):
            await asyncio.sleep(0)
        with pytest.raises(SnapshotInUseError):
            store.delete_snapshot(path.name)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("domain", ["automation", "script", "scene"])
async def test_config_restore_reads_and_writes_stored_key_without_alias_resolution(
    runtime, monkeypatch, domain
):
    from custom_components.ha_mcp_tools.native_operations import core_views
    from ha_mcp.tool_api import CallContext

    manager, snapshots, store, _path, _rows, _calls = runtime
    current = {"name": "Current"}
    recorded = {"name": "Recorded"}
    if domain in {"automation", "scene"}:
        recorded["id"] = "storage_id"
    read = AsyncMock(return_value=current)
    monkeypatch.setattr(core_views, "read_config_item", read)
    manager.backend.execute = AsyncMock(return_value={"result": "ok"})
    path = store.capture(domain, "storage_id", recorded, "fixture")
    result = await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert result["apply_status"] == "applied"
    read.assert_awaited_once_with(manager.hass, domain, "storage_id")
    manager.backend.execute.assert_awaited_once_with(
        f"config.{domain}.write",
        {
            "storage_key": "storage_id",
            "identifier": "storage_id",
            "expected_config": current,
            "data": recorded,
        },
        CallContext("owner"),
    )


async def test_todo_capture_uses_prepared_item_without_a_second_live_read(runtime):
    from ha_mcp.tool_api import CallContext

    manager, snapshots, store, _path, _rows, calls = runtime
    item = {"uid": "stable", "summary": "Buy milk", "status": "needs_action"}
    await snapshots.todo_backup(
        {
            "entity_id": "todo.shopping",
            "item": "Buy milk",
            "current_item": item,
            "source_tool": "ha_remove_todo_item",
        },
        CallContext("owner"),
    )
    entries = store.list_snapshots(domain="todo_item")
    assert len(entries) == 1
    record = store.read_snapshot(entries[0]["name"])
    assert record["entity_id"] == "todo.shopping::Buy milk"
    assert record["config"] == {"todo_entity_id": "todo.shopping", **item}
    assert not calls


async def test_core_yaml_nodes_can_be_rendered_in_the_backup_panel(
    runtime, monkeypatch
):
    from custom_components.ha_mcp_tools.native_operations import core_views
    from ha_mcp.runtime.snapshot_projection import panel_diff
    from ha_mcp.tool_api import CallContext

    class NodeDict(dict):
        pass

    manager, snapshots, store, _path, _rows, _calls = runtime
    current = NodeDict(sequence=[NodeDict(action="light.turn_off")])
    monkeypatch.setattr(core_views, "read_config_item", AsyncMock(return_value=current))
    path = store.capture(
        "script", "script.test", {"sequence": [{"action": "light.turn_on"}]}, "fixture"
    )
    data, live = await snapshots.snapshot_comparison(path.name, CallContext("owner"))
    result = await snapshots._io(panel_diff, path.name, data, live)
    assert result["success"]
    assert "+- action: light.turn_off" in result["diff"]
    assert "-- action: light.turn_on" in result["diff"]
