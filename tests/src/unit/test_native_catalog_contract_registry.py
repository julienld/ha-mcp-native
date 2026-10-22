"""Native device discovery joins Core owners without a transport facade."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.devices import DeviceDiscoveryTools
from ha_mcp.runtime.entities import EntityDiscoveryTools
from ha_mcp.tool_api import CallContext
from ha_mcp.tools.tool_hints import read_only_hints

from .test_component_ws_search import (
    FakeDevice,
    FakeErModule,
    FakeHass,
    FakeRegEntry,
    make_view,
    wsapi,
)
from .test_native_engine_backend import authorize
from .test_native_engine_backend import native as native


async def test_native_device_lookup_keeps_disabled_entities_and_reports_missing_radio(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable optional radio must not hide the Core device or its entities."""
    entity = FakeRegEntry(
        "sensor.disabled",
        device_id="device",
        original_name="Temperature",
        platform="zha",
        disabled_by="user",
    )
    view = make_view(
        entity={entity.entity_id: entity},
        devices=[FakeDevice("device", name="Probe", identifiers=(("zha", "00:11"),))],
    )
    monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: view)
    monkeypatch.setattr(wsapi, "er", FakeErModule())
    registry_module = SimpleNamespace(async_get=lambda hass: view.entity)
    monkeypatch.setitem(
        sys.modules, "homeassistant.helpers.entity_registry", registry_module
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"], "entity_registry", registry_module
    )
    validator = Mock(return_value=entity.entity_id)
    validation_module = SimpleNamespace(entity_id=validator)
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"], "config_validation", validation_module
    )
    hass = FakeHass(config=SimpleNamespace(components=set()))
    authorize(hass)
    backend = native.NativeBackend(hass)
    definition = define_tool(
        backend,
        DeviceDiscoveryTools,
        "ha_get_device",
        annotations=read_only_hints("Get Device", open_world=False),
    )
    result = await Engine(backend, [definition]).call(
        "ha_get_device", {"entity_id": "SENSOR.DISABLED"}, CallContext("actor")
    )
    assert not result.is_error
    validator.assert_called_once_with("SENSOR.DISABLED")
    assert result.data["device"]["device_id"] == "device"
    assert result.data["entities"] == [
        {"entity_id": "sensor.disabled", "name": "Temperature", "platform": "zha"}
    ]
    assert "radio_metrics" not in result.data["device"]
    assert result.data["warnings"] == [
        "ZHA radio metrics unavailable: zha integration is not loaded"
    ]


async def test_native_bulk_entity_read_keeps_duplicate_order_and_extended_metadata(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extended reads retain aliases/options while an absent ID remains a row error."""
    entry = FakeRegEntry("light.a", device_id="device", name="", unique_id="stable")
    entry.extended_dict = {
        **entry.as_partial_dict,
        "aliases": [None, "Desk"],
        "device_class": "outlet",
        "options": {"light": {"default_transition": 4}},
    }
    view = make_view(entity={entry.entity_id: entry})
    monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: view)
    registry_module = SimpleNamespace(async_get=lambda hass: view.entity)
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"], "entity_registry", registry_module
    )
    validator = Mock(side_effect=lambda value: value)
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"],
        "config_validation",
        SimpleNamespace(entity_ids=validator),
    )
    hass = FakeHass(config=SimpleNamespace(components=set()))
    authorize(hass)
    backend = native.NativeBackend(hass)
    definition = define_tool(
        backend,
        EntityDiscoveryTools,
        "ha_get_entity",
        annotations=read_only_hints("Get Entity", open_world=False),
    )
    result = await Engine(backend, [definition]).call(
        "ha_get_entity",
        {"entity_id": ["light.a", "light.missing", "light.a"]},
        CallContext("actor"),
    )
    assert not result.is_error
    assert [row["entity_id"] for row in result.data["entity_entries"]] == [
        "light.a",
        "light.a",
    ]
    row = result.data["entity_entries"][0]
    assert row["aliases"] == [None, "Desk"]
    assert row["device_class"] == "outlet" and row["uses_device_name"] is True
    assert row["options"] == {"light": {"default_transition": 4}}
    assert result.data["errors"] == [
        {"entity_id": "light.missing", "error": "Entity not found"}
    ]
    validator.assert_called_once_with(["light.a", "light.missing", "light.a"])
