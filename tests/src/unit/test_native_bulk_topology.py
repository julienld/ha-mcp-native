"""Bulk snapshots retain Core's main/child device enumeration and serializers."""

import importlib
import json
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from ha_mcp.runtime.bulk_selection import prepare_bulk_selection, resolve_bulk_selection
from ha_mcp.tool_api import CallContext

from .test_native_engine_backend import native as native


@pytest.mark.parametrize(
    "with_children", [False, True], ids=["core-august", "core-current"]
)
def test_bulk_topology_resolves_registry_shapes_and_detaches_owner_values(
    native: Any, monkeypatch: pytest.MonkeyPatch, with_children: bool
) -> None:
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.bulk_control"
    )
    device_rows = [
        {"id": "parent", "area_id": "room"},
        {"id": "child", "parent_device_id": "parent", "area_id": None},
    ]
    devices = SimpleNamespace(
        devices=[
            SimpleNamespace(json_repr=json.dumps(device_rows[0]).encode()),
            SimpleNamespace(json_repr=None),
        ],
        child_devices=[SimpleNamespace(json_repr=json.dumps(device_rows[1]).encode())],
    )
    if not with_children:
        device_rows = [{"id": "child", "area_id": "room"}]
        devices = SimpleNamespace(
            devices={
                "child": SimpleNamespace(json_repr=json.dumps(device_rows[0]).encode())
            }
        )
    attrs = {"friendly_name": "Child light"}
    hass = SimpleNamespace(
        states=SimpleNamespace(
            async_all=lambda: [
                SimpleNamespace(
                    as_dict=lambda: {
                        "entity_id": "light.child",
                        "state": "off",
                        "attributes": attrs,
                    }
                )
            ]
        )
    )
    entity = {"entity_id": "light.child", "device_id": "child", "area_id": None}
    registries = {
        "entity": SimpleNamespace(
            entities={"entity": SimpleNamespace(as_partial_dict=entity)}
        ),
        "device": devices,
        "area": SimpleNamespace(
            areas={
                "room": SimpleNamespace(
                    json_fragment={"area_id": "room", "floor_id": "floor"}
                )
            }
        ),
        "floor": SimpleNamespace(floors={"floor": SimpleNamespace(id="floor")}),
    }
    for name, registry in registries.items():
        owner = SimpleNamespace(async_get=lambda _hass, registry=registry: registry)
        monkeypatch.setattr(
            sys.modules["homeassistant.helpers"],
            name + "_registry",
            owner,
            raising=False,
        )
        monkeypatch.setitem(
            sys.modules, "homeassistant.helpers." + name + "_registry", owner
        )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.config.floor_registry",
        SimpleNamespace(
            _entry_dict=lambda entry: {"floor_id": entry.id, "future_field": True}
        ),
    )
    service = module.NativeBulkControl(SimpleNamespace(hass=hass), CallContext("actor"))
    image = service._topology(include_registries=True)
    assert image.devices == device_rows and image.floors[0]["future_field"]
    attrs["friendly_name"] = "Changed"
    assert image.states[0]["attributes"]["friendly_name"] == "Child light"
    prepared = prepare_bulk_selection(
        image, {"domain": "light", "area_ids": ["room"]}, "on"
    )
    resolution = resolve_bulk_selection(
        prepared,
        hidden=set(),
        visibility_warnings=[],
        parameters=None,
        timeout_seconds=None,
        validate_first=True,
    )
    assert resolution.resolved_entity_ids == ("light.child",)
