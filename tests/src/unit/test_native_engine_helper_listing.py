"""Helper inventory and descriptions use native owners through the shared tool."""

import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.helper_listing import HelperListingTools, description_starts_flow
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.tools.tool_hints import write_hints

from .test_component_helper_collections import FakeCollection, FakeRegistry, _hass
from .test_component_ws_search import make_view, wsapi
from .test_native_engine_backend import authorize
from .test_native_engine_backend import native as native
from .test_native_engine_flows import flows as flows
from .test_native_engine_full_flows import _setup


def _engine(backend: Any) -> Engine:
    definition = define_tool(
        backend,
        HelperListingTools,
        "ha_config_list_helpers",
        annotations=write_hints(
            "List Helpers", destructive=True, idempotent=False, open_world=False
        ),
        writes=description_starts_flow,
    )
    return Engine(backend, [definition])


async def test_native_tags_are_complete_paginated_and_available_readonly(
    native: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    collection = FakeCollection(
        {"one": {"id": "one", "name": "One"}, "two": {"id": "two", "name": "Two"}}
    )
    hass = _hass(collection, "tag")
    hass.config = SimpleNamespace(path=lambda name: str(tmp_path / name))
    authorize(hass)
    monkeypatch.setattr(wsapi.er, "async_get", lambda hass: FakeRegistry())
    monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: make_view())
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.loader",
        SimpleNamespace(async_get_config_flows=AsyncMock(return_value=set())),
    )
    engine = _engine(native.NativeBackend(hass))
    actor = CallContext("actor", read_only=True)
    page = await engine.call(
        "ha_config_list_helpers", {"helper_type": "tag", "limit": 1, "offset": 1}, actor
    )
    assert not page.is_error, page.data
    assert page.data["helpers"] == [{"id": "two", "name": "Two"}]
    assert page.data["total_count"] == 2 and page.data["count"] == 1
    all_types = await engine.call(
        "ha_config_list_helpers", {"helper_type": "all"}, actor
    )
    assert not all_types.is_error, all_types.data
    assert {row["id"] for row in all_types.data["helpers"]} == {"one", "two"}
    assert {row["helper_type"] for row in all_types.data["helpers"]} == {"tag"}


async def test_tag_names_after_restart_come_from_registry_without_changing_cas_data(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .test_native_engine_helpers import helper_engine

    collection = FakeCollection({"stable-id": {"id": "stable-id", "migrated": True}})
    hass = _hass(collection, "tag")
    authorize(hass)
    registry = FakeRegistry()
    registry.add("tag.current_name", "tag", "stable-id")
    registry.entries["tag.current_name"].name = "Current Name"
    registry.entries["tag.current_name"].original_name = "Tag stable-id"
    monkeypatch.setattr(wsapi.er, "async_get", lambda hass: registry)
    monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: make_view())
    backend = native.NativeBackend(hass)
    actor = CallContext("actor")
    storage = await backend.execute(
        "helpers.storage.list", {"helper_type": "tag"}, actor
    )
    assert storage == {"items": [{"id": "stable-id", "name": "Current Name"}]}
    listed = await _engine(backend).call(
        "ha_config_list_helpers", {"helper_type": "tag"}, actor
    )
    assert listed.data["helpers"] == [
        {
            "id": "stable-id",
            "name": "Current Name",
            "entity_id": "tag.current_name",
            "original_name": "Current Name",
        }
    ]
    duplicate = await helper_engine(backend).call(
        "ha_config_set_helper",
        {"helper_type": "tag", "name": "Current Name", "MandatoryBPS": False},
        actor,
    )
    assert duplicate.is_error and "already exists" in str(duplicate.data)
    assert collection.data == {"stable-id": {"id": "stable-id", "migrated": True}}


async def test_native_storage_description_uses_core_schema_and_exact_live_item(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    collection = FakeCollection({"stored": {"id": "stored", "name": "Existing"}})
    hass = _hass(collection, "input_number")
    authorize(hass)
    registry = FakeRegistry()
    registry.add("input_number.renamed", "input_number", "stored")
    monkeypatch.setattr(wsapi.er, "async_get", lambda hass: registry)
    result = await _engine(native.NativeBackend(hass)).call(
        "ha_config_list_helpers",
        {
            "helper_type": "input_number",
            "describe": True,
            "helper_id": "input_number.renamed",
        },
        CallContext("actor", read_only=True),
    )
    assert not result.is_error, result.data
    assert result.data["source"] == "core_schema"
    assert (
        next(row for row in result.data["fields"] if row["name"] == "name")["current"]
        == "Existing"
    )


async def test_flow_description_has_explicit_effect_and_reuses_core_form(
    native: Any, flows: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, _, manager, backend, _ = _setup(native, monkeypatch)
    manager.async_init.return_value = {
        "flow_id": "f",
        "type": "form",
        "step_id": "user",
        "last_step": True,
        "data_schema": [{"name": "name", "type": "string"}],
    }
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.helpers.translation",
        SimpleNamespace(
            async_get_translations=AsyncMock(
                return_value={
                    "component.template.config.step.user.data.name": "Name from Core"
                }
            )
        ),
    )
    engine = _engine(backend)
    with pytest.raises(ToolFailure, match="Writes are disabled"):
        await engine.call(
            "ha_config_list_helpers",
            {"helper_type": "template", "describe": True},
            CallContext("actor", read_only=True),
        )
    manager.async_init.assert_not_awaited()
    result = await engine.call(
        "ha_config_list_helpers",
        {"helper_type": "template", "describe": True},
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert result.data["fields"] == [
        {"name": "name", "type": "string", "description": "Name from Core"}
    ]
    manager.async_abort.assert_called_once_with("f")
    assert not backend._flows.owned
