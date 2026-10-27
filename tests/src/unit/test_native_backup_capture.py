"""On-demand capture preserves domain identities and the actual stored shape."""

import importlib
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tool_api import CallContext

from .test_component_helper_collections import FakeCollection, FakeRegistry, _hass
from .test_native_engine_backend import native as native


@pytest.fixture
def capture(native: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    collection = FakeCollection(
        {"stable": {"id": "stable", "name": "Stored", "future": [1, 2]}}
    )
    hass = _hass(collection, "input_boolean")
    registry = FakeRegistry()
    registry.add("input_boolean.renamed", "input_boolean", "stable")
    registry.entries["input_boolean.renamed"].icon = "mdi:lamp"
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers.entity_registry"],
        "async_get",
        lambda _hass: registry,
    )
    manager = SimpleNamespace(
        hass=hass, backend=SimpleNamespace(hass=hass, authorize=AsyncMock())
    )
    from custom_components.ha_mcp_tools.native_snapshots import NativeSnapshots

    manager.snapshots = NativeSnapshots(manager)
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.backup_capture"
    )
    source = module.NativeCaptureSources(manager, CallContext("actor"))
    return SimpleNamespace(
        source=source, manager=manager, collection=collection, registry=registry
    )


async def test_capture_resolves_storage_alias_and_keeps_registry_icon_and_future_fields(
    capture: Any,
) -> None:
    identifier, data = await capture.source.read(
        "helper_input_boolean", "input_boolean.renamed"
    )
    assert identifier == "input_boolean.renamed"
    assert data == {
        "id": "stable",
        "name": "Stored",
        "future": [1, 2],
        "registry_icon": "mdi:lamp",
    }
    capture.collection.data["stable"]["future"].append(3)
    assert data["future"] == [1, 2]


async def test_capture_todo_stores_item_values_not_the_internal_selector_envelope(
    capture: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.ha_mcp_tools.native_operations.todo import (
        NativeTodoOperations,
    )

    row = {
        "uid": "stable-uid",
        "summary": "Milk",
        "status": "needs_action",
        "future": True,
    }
    monkeypatch.setattr(NativeTodoOperations, "_item_list", lambda _self, entity: [row])
    _identifier, data = await capture.source.read("todo_item", "todo.shopping::Milk")
    assert data == {"todo_entity_id": "todo.shopping", **row}
    assert "current_item" not in data and "selector" not in data


async def test_capture_renamed_script_keeps_its_storage_identity_for_later_restore(
    capture: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.ha_mcp_tools.native_operations import config_reads

    reader = AsyncMock(
        return_value={
            "found": True,
            "storage_key": "original_storage_key",
            "config": {"alias": "Renamed", "sequence": []},
        }
    )
    monkeypatch.setattr(config_reads, "_script_read", reader)
    identifier, config = await capture.source.read("script", "script.renamed_entity")
    assert identifier == "original_storage_key"
    assert config == {"alias": "Renamed", "sequence": []}


async def test_flow_entity_alias_returns_canonical_entry_id_without_starting_a_flow(
    capture: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.ha_mcp_tools.native_operations import snapshot_sources

    entry = SimpleNamespace(entry_id="actual-entry", domain="template")
    capture.registry.entries["sensor.helper"] = SimpleNamespace(
        config_entry_id=entry.entry_id
    )
    capture.manager.hass.config_entries = SimpleNamespace(
        async_get_entry=lambda key: entry if key == entry.entry_id else None
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.loader",
        SimpleNamespace(async_get_config_flows=AsyncMock(return_value={"template"})),
    )
    snapshot = {"entry_id": entry.entry_id, "options": {"state": "12"}, "entities": []}
    producer = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(snapshot_sources, "flow_snapshot", producer)
    identifier, result = await capture.source.read("helper_template", "sensor.helper")
    assert identifier == entry.entry_id and result is snapshot
    producer.assert_awaited_once_with(
        capture.manager.hass, {"helper_type": "template", "entry_id": entry.entry_id}
    )
