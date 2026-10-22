"""Native zone writes keep stored targets, snapshots and single dispatch."""

import copy
import importlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.backends.remote import RemoteBackend
from ha_mcp.runtime import Engine
from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.zone_writes import ZoneWriteTools
from ha_mcp.tool_api import CallContext

from .test_component_helper_collections import FakeCollection, FakeRegistry, _hass
from .test_component_ws_search import FakeRegEntry, FakeState, wsapi
from .test_native_engine_backend import EventBus, authorize
from .test_native_engine_backend import native as native


class _Zones(FakeCollection):
    def __init__(self) -> None:
        super().__init__(
            {
                "office": {
                    "id": "office",
                    "name": "Office",
                    "latitude": 1,
                    "longitude": 2,
                    "radius": 100,
                    "passive": False,
                }
            }
        )
        self.deletes = 0
        self.fail_after_delete = False

    async def async_delete_item(self, item_id: str) -> None:
        self.deletes += 1
        self.data.pop(item_id)
        if self.fail_after_delete:
            raise RuntimeError("listener failed after deletion")


def _setup(native: Any, monkeypatch: pytest.MonkeyPatch, backup: Any) -> tuple:
    operations = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.zones"
    )
    monkeypatch.setattr(operations, "callback", lambda function: function)
    collection = _Zones()
    registry = FakeRegistry()
    entry = FakeRegEntry("zone.renamed", platform="zone", unique_id="office")
    entry.icon = "mdi:briefcase"
    registry.entries[entry.entity_id] = entry
    registry.entities = registry.entries
    monkeypatch.setattr(wsapi.er, "async_get", lambda hass: registry)
    monkeypatch.setattr(operations.entity_registry, "async_get", lambda hass: registry)
    hass = _hass(collection, "zone")
    hass.bus = EventBus()
    authorize(hass)
    backend = native.NativeBackend(hass, helper_backup=backup)
    engine = Engine(
        backend,
        [
            define_tool(
                backend, ZoneWriteTools, name, annotations={"readOnlyHint": False}
            )
            for name in ("ha_set_zone", "ha_remove_zone")
        ],
    )
    return engine, collection, hass


async def test_native_zone_update_and_remove_snapshot_the_renamed_stored_target(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = []

    async def backup(payload: dict, context: CallContext) -> dict:
        captured.append(copy.deepcopy(payload))
        return {"warnings": ["Backup disk nearly full"]}

    engine, collection, hass = _setup(native, monkeypatch, backup)
    updated = await engine.call(
        "ha_set_zone", {"zone_id": "zone.renamed", "radius": 250}, CallContext("actor")
    )
    assert not updated.is_error, updated.data
    assert updated.data["zone_id"] == "office"
    assert updated.data["zone_data"]["icon"] == "mdi:briefcase"
    assert updated.data["updated_fields"] == ["radius"]
    assert captured[0]["current_item"]["radius"] == 100
    assert captured[0]["backup_domain"] == "zone"
    assert captured[0]["source_tool"] == "ha_set_zone"
    removed = await engine.call(
        "ha_remove_zone", {"zone_id": "zone.renamed"}, CallContext("actor")
    )
    assert not removed.is_error, removed.data
    assert removed.data["zone_id"] == "office"
    assert removed.data["warnings"] == ["Backup disk nearly full"]
    assert captured[1]["current_item"]["radius"] == 250
    assert captured[1]["source_tool"] == "ha_remove_zone"
    assert collection.data == {} and collection.deletes == 1
    assert hass.bus.listeners == []


async def test_native_zone_create_preserves_zone_response_and_registry_icon(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = AsyncMock()
    engine, collection, hass = _setup(native, monkeypatch, backup)
    registry = wsapi.er.async_get(hass)
    entry = FakeRegEntry("zone.garden", platform="zone", unique_id="garden")
    entry.icon = None
    registry.entries[entry.entity_id] = entry
    result = await engine.call(
        "ha_set_zone",
        {
            "name": "Garden",
            "latitude": 45.5,
            "longitude": -73.5,
            "icon": "mdi:tree",
            "wait": False,
        },
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert result.data["zone_id"] == "garden"
    assert result.data["entity_id"] == "zone.garden"
    assert result.data["zone_data"]["icon"] == "mdi:tree"
    assert collection.data["garden"] == {
        "id": "garden",
        "name": "Garden",
        "latitude": 45.5,
        "longitude": -73.5,
    }
    assert registry.updates == [("zone.garden", {"icon": "mdi:tree"})]
    backup.assert_not_awaited()


async def test_native_zone_delete_refuses_a_target_changed_during_snapshot(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def backup(payload: dict, context: CallContext) -> None:
        collection.data["office"]["radius"] = 999

    engine, collection, _ = _setup(native, monkeypatch, backup)
    result = await engine.call(
        "ha_remove_zone", {"zone_id": "office", "wait": False}, CallContext("actor")
    )
    assert result.is_error and result.data["error"]["code"] == "CONFIG_CONFLICT"
    assert collection.deletes == 0 and collection.data["office"]["radius"] == 999


async def test_native_zone_home_is_never_deleted_or_snapshotted(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = AsyncMock()
    engine, collection, hass = _setup(native, monkeypatch, backup)
    monkeypatch.setattr(hass.states, "get", lambda entity_id: FakeState("zone.home"))
    result = await engine.call(
        "ha_remove_zone", {"zone_id": "zone.home"}, CallContext("actor")
    )
    assert result.is_error and result.data["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert "general settings" in result.data["error"]["message"]
    assert collection.deletes == 0
    backup.assert_not_awaited()


async def test_native_zone_delete_is_not_replayed_after_a_listener_failure(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, collection, _ = _setup(native, monkeypatch, AsyncMock(return_value={}))
    collection.fail_after_delete = True
    result = await engine.call(
        "ha_remove_zone", {"zone_id": "office"}, CallContext("actor")
    )
    assert result.is_error and result.status == "outcome_unknown"
    assert collection.deletes == 1 and collection.data == {}


async def test_native_zone_delete_rechecks_readonly_after_snapshot(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def backup(payload: dict, context: CallContext) -> None:
        engine.read_only = True

    engine, collection, _ = _setup(native, monkeypatch, backup)
    result = await engine.call(
        "ha_remove_zone", {"zone_id": "office"}, CallContext("actor")
    )
    assert result.is_error and result.data["error"]["code"] == "READ_ONLY"
    assert collection.deletes == 0 and "office" in collection.data


@pytest.mark.parametrize("concurrent_edit", [False, True])
async def test_remote_zone_delete_uses_existing_client_and_compares_snapshot(
    monkeypatch: pytest.MonkeyPatch, concurrent_edit: bool
) -> None:
    from ha_mcp.tools import component_helper_collections

    monkeypatch.setattr(
        component_helper_collections, "_component_ws", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "ha_mcp.config.get_global_settings",
        lambda: SimpleNamespace(enable_auto_backup=False),
    )
    stored = {
        "id": "office",
        "name": "Office",
        "latitude": 1,
        "longitude": 2,
        "radius": 100,
    }
    row = {"entity_id": "zone.renamed", "platform": "zone", "unique_id": "office"}
    lists = 0
    deletes = 0

    async def send(message: dict) -> dict:
        nonlocal lists, deletes
        if message["type"] == "zone/list":
            lists += 1
            if concurrent_edit and lists == 3:
                stored["radius"] = 999
            return {"success": True, "result": [copy.deepcopy(stored)]}
        if message["type"] == "config/entity_registry/list":
            return {"success": True, "result": [row]}
        if message["type"] == "zone/delete":
            deletes += 1
            assert message["zone_id"] == "office"
            stored.clear()
            return {"success": True, "result": None}
        raise AssertionError(message)

    backend = RemoteBackend(
        SimpleNamespace(send_websocket_message=AsyncMock(side_effect=send))
    )
    definition = define_tool(
        backend, ZoneWriteTools, "ha_remove_zone", annotations={"readOnlyHint": False}
    )
    result = await Engine(backend, [definition]).call(
        "ha_remove_zone",
        {"zone_id": "zone.renamed", "wait": False},
        CallContext("remote"),
    )
    if concurrent_edit:
        assert result.is_error and result.data["error"]["code"] == "CONFIG_CONFLICT"
        assert deletes == 0 and stored["radius"] == 999
    else:
        assert not result.is_error, result.data
        assert result.data["zone_id"] == "office" and deletes == 1 and stored == {}
