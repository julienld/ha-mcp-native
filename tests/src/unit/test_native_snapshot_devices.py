"""Device history uses Core updates and preserves the full safety before-image."""

import asyncio
import importlib
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime.snapshot_payloads import BackupRestoreError
from ha_mcp.tool_api import CallContext

from .test_native_devices import _Child, _Devices, _Disabler
from .test_native_snapshot_restore import runtime as runtime


@pytest.fixture
def devices(runtime, monkeypatch):
    manager, snapshots, store, *_ = runtime
    registry = _Devices()
    for row in registry.rows.values():
        row.labels = set()
    module = SimpleNamespace(
        async_get=lambda hass: registry,
        ChildDeviceEntry=_Child,
        DeviceEntryDisabler=_Disabler,
    )
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.device_registry", module)
    monkeypatch.setattr(sys.modules["homeassistant.helpers"], "device_registry", module)
    labels = SimpleNamespace(async_get_missing_label_ids=lambda hass, ids: set())
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.label_registry", labels)
    monkeypatch.setattr(sys.modules["homeassistant.helpers"], "label_registry", labels)
    access = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.registry_access"
    )
    monkeypatch.setattr(access, "json_bytes", lambda value: json.dumps(value).encode())
    manager.hass.data = {
        "websocket_api": {"config/device_registry/update": (None, dict)}
    }
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.devices"
    )
    capture = AsyncMock(side_effect=AssertionError("Unexpected second capture"))
    monkeypatch.setattr(module, "capture_registry_before", capture)
    saved = {**registry.rows["main"].dict_repr, "name_by_user": "Saved"}
    path = store.capture("device", "main", saved, "fixture")
    return snapshots, store, path, registry, capture


async def test_device_diff_and_restore_capture_one_full_safety_image(devices):
    snapshots, store, path, registry, capture = devices
    before = registry.rows["main"].dict_repr
    data, current = await snapshots.snapshot_comparison(path.name, CallContext("owner"))
    assert data["config"]["name_by_user"] == "Saved" and current == before
    result = await asyncio.wait_for(
        snapshots.restore_snapshot(path.name, CallContext("owner")), 2
    )
    assert result["apply_status"] == "applied"
    assert registry.rows["main"].name_by_user == "Saved"
    assert registry.rows["main"].name == "From integration"
    assert set(registry.calls[0][1]) == {
        "device_id",
        "name_by_user",
        "area_id",
        "disabled_by",
        "labels",
    }
    assert store.read_snapshot(result["safety_backup"])["config"] == before
    assert len(store.list_snapshots(domain="device")) == 2
    capture.assert_not_awaited()


@pytest.mark.parametrize("change", ["recreated", "typed_value"])
async def test_device_changed_during_safety_io_is_not_overwritten(
    devices, monkeypatch, change
):
    snapshots, _store, path, registry, _capture = devices
    io = snapshots._io

    async def changed(function, *args):
        result = await io(function, *args)
        if function.__name__ == "capture":
            if change == "recreated":
                registry.rows["main"] = registry.new("main")
                registry.rows["main"].created_at = 3.0
            else:
                registry.rows["main"].future_core_field = 0
        return result

    monkeypatch.setattr(snapshots, "_io", changed)
    with pytest.raises(BackupRestoreError) as error:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert error.value.outcome["apply_status"] == "not_applied"
    assert error.value.outcome["safety_backup"]
    assert not registry.calls


async def test_deleted_device_restore_explains_integration_ownership(devices):
    snapshots, _store, path, registry, _capture = devices
    del registry.rows["main"]
    with pytest.raises(
        BackupRestoreError, match="integration owns device creation"
    ) as error:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert error.value.outcome["apply_status"] == "not_applied"
    assert error.value.outcome["reason"] == "entry_deleted"
    assert not registry.calls


async def test_composite_restore_explains_why_it_is_refused(devices):
    snapshots, store, _path, registry, _capture = devices
    path = store.capture(
        "device", "composite", registry.rows["composite"].dict_repr, "fixture"
    )
    with pytest.raises(BackupRestoreError, match="composite device") as error:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert error.value.outcome["apply_status"] == "not_applied"
    assert not registry.calls
