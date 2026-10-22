"""Label history restores through its Core service, with one safety image."""

import asyncio
import copy
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime.snapshot_payloads import BackupRestoreError
from ha_mcp.tool_api import CallContext

from .test_native_snapshot_restore import runtime as runtime


@pytest.fixture
def labels(runtime, monkeypatch):
    from custom_components.ha_mcp_tools.native_operations import labels as module

    manager, snapshots, store, *_rest = runtime
    current = {
        "label_id": "light",
        "name": "Current",
        "color": "red",
        "icon": None,
        "description": None,
        "created_at": 1.0,
        "modified_at": 2.0,
    }
    rows = {"light": current}
    writes = []

    def update(**fields):
        writes.append(fields)
        rows[fields["label_id"]].update(fields)
        return rows[fields["label_id"]]

    registry = SimpleNamespace(async_get_label=rows.get, async_update=update)
    monkeypatch.setattr(
        module.NativeLabels,
        "_label_owner",
        lambda _: (registry, copy.deepcopy),
    )
    access = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.registry_access"
    )
    monkeypatch.setattr(access, "json_bytes", lambda value: json.dumps(value).encode())
    manager.hass.data = {
        "websocket_api": {
            "config/label_registry/update": (None, dict)
        }
    }
    capture = AsyncMock(
        side_effect=AssertionError(
            "Restore must not auto-capture after its safety image"
        )
    )
    monkeypatch.setattr(module.NativeLabels, "_capture", capture)
    saved = {**current, "name": "Saved", "color": "blue", "modified_at": 1.0}
    path = store.capture("label", "light", saved, "fixture")
    return snapshots, store, path, rows, writes, capture


async def test_label_diff_and_restore_use_core_and_capture_one_safety_image(labels):
    snapshots, store, path, rows, writes, capture = labels
    original = copy.deepcopy(rows["light"])
    data, current = await snapshots.snapshot_comparison(path.name, CallContext("owner"))
    assert data["config"]["name"] == "Saved" and current == original
    result = await asyncio.wait_for(
        snapshots.restore_snapshot(path.name, CallContext("owner")), 2
    )
    assert result["apply_status"] == "applied"
    assert rows["light"]["name"] == "Saved" and rows["light"]["color"] == "blue"
    assert "created_at" not in writes[0] and "modified_at" not in writes[0]
    assert store.read_snapshot(result["safety_backup"])["config"] == original
    assert len(store.list_snapshots(domain="label")) == 2
    capture.assert_not_awaited()


async def test_label_change_while_safety_is_saved_refuses_without_core_write(
    labels, monkeypatch
):
    snapshots, store, path, rows, writes, _capture = labels
    io = snapshots._io

    async def changed(function, *args):
        result = await io(function, *args)
        if function.__name__ == "capture":
            rows["light"] = {
                **rows["light"],
                "name": "Recreated label",
                "created_at": 3.0,
            }
        return result

    monkeypatch.setattr(snapshots, "_io", changed)
    with pytest.raises(BackupRestoreError) as error:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert error.value.outcome["apply_status"] == "not_applied"
    assert error.value.outcome["safety_backup"]
    assert rows["light"]["name"] == "Recreated label" and not writes


async def test_deleted_label_restore_explains_its_identity_limit(labels):
    snapshots, _store, path, rows, writes, _capture = labels
    rows.clear()
    with pytest.raises(
        BackupRestoreError, match="cannot recreate the original label identity"
    ) as error:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert error.value.outcome["apply_status"] == "not_applied"
    assert error.value.outcome["reason"] == "entry_deleted"
    assert not writes and not rows
