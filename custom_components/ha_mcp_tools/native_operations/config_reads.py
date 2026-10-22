"""Fresh persisted configuration reads through Core's registered owners."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.core import HomeAssistant

from ha_mcp.tool_api import ToolFailure, UnsupportedOperation

from .core_views import read_config_item


def _registry_entry(hass: HomeAssistant, entity_id: str) -> Any:
    from homeassistant.helpers import config_validation as cv
    from homeassistant.helpers import entity_registry as er

    from ..helper_collections import _INVALID_ERRORS

    try:
        entity_id = cv.entity_id(entity_id)
    except _INVALID_ERRORS:
        return None
    return er.async_get(hass).async_get(entity_id)


def _available_ids(hass: HomeAssistant, domain: str) -> list[str]:
    from homeassistant.helpers import entity_registry as er

    ids = [
        entry.entity_id
        for entry in er.async_get(hass).entities.values()
        if entry.entity_id.startswith(f"{domain}.")
    ][:10]
    return (
        [entity_id.removeprefix("script.") for entity_id in ids]
        if domain == "script"
        else ids
    )


async def _automation_read(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> dict[str, Any]:
    identifier = arguments["identifier"]
    storage_key = identifier
    if identifier.startswith("automation."):
        state = hass.states.get(identifier)
        storage_key = state.attributes.get("id") if state else None
    value = (
        await read_config_item(hass, "automation", str(storage_key))
        if storage_key
        else None
    )
    return {
        "found": value is not None,
        "config": value,
        "storage_key": storage_key,
        "available_ids": _available_ids(hass, "automation") if value is None else [],
    }


async def _script_read(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> dict[str, Any]:
    identifier = arguments["identifier"]
    bare = identifier.removeprefix("script.")
    entry = _registry_entry(hass, f"script.{bare}")
    key = str(entry.unique_id) if entry and entry.unique_id else bare
    value = await read_config_item(hass, "script", key)
    return {
        "found": value is not None,
        "config": value,
        "storage_key": key,
        "available_ids": _available_ids(hass, "script") if value is None else [],
    }


async def _scene_read(hass: HomeAssistant, arguments: dict[str, Any]) -> dict[str, Any]:
    supplied = arguments.get("resolution")
    if supplied is None:
        supplied = await _scene_resolve(hass, arguments)
    key, hit, platform = (
        supplied["storage_key"],
        supplied["registry_hit"],
        supplied["platform"],
    )
    value = await read_config_item(hass, "scene", key)
    return {
        "found": value is not None,
        "config": value,
        "storage_key": key,
        "not_in_storage": value is None
        and (hit or hass.states.get(f"scene.{key}") is not None),
        "platform": platform,
    }


async def _scene_resolve(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> dict[str, Any]:
    bare = arguments["identifier"].removeprefix("scene.")
    entry = _registry_entry(hass, f"scene.{bare}")
    hit = bool(entry and entry.unique_id)
    return {
        "storage_key": str(entry.unique_id) if hit else bare,
        "registry_hit": hit,
        "platform": entry.platform if hit else None,
    }


async def _entity_id(hass: HomeAssistant, arguments: dict[str, Any]) -> str | None:
    from homeassistant.helpers import entity_registry as er

    identifier: str = arguments["identifier"]
    domain = arguments["domain"]
    if domain == "automation":
        if identifier.startswith("automation."):
            return identifier
        return next(
            (
                state.entity_id
                for state in hass.states.async_all()
                if state.entity_id.startswith("automation.")
                and state.attributes.get("id") == identifier
            ),
            None,
        )
    if domain not in {"script", "scene"}:
        raise UnsupportedOperation(f"config.{domain}.entity_id")
    key = identifier.removeprefix(f"{domain}.")
    for attempt in range(2 if domain == "scene" else 1):
        for entry in er.async_get(hass).entities.values():
            if entry.entity_id.startswith(f"{domain}.") and entry.unique_id == key:
                return str(entry.entity_id)
        if domain == "scene" and attempt == 0:
            await asyncio.sleep(0.2)
    return f"{domain}.{key}"


async def _category(hass: HomeAssistant, arguments: dict[str, Any]) -> dict[str, Any]:
    entity_id = arguments["entity_id"]
    entry = _registry_entry(hass, entity_id)
    if entry is None:
        return {
            "category": None,
            "warnings": [f"category unavailable for {entity_id}: Entity not found"],
        }
    category = entry.categories.get(arguments["scope"])
    return {"category": str(category) if category is not None else None, "warnings": []}


async def _zones(hass: HomeAssistant, arguments: dict[str, Any]) -> dict[str, Any]:
    from homeassistant.helpers import entity_registry as er

    from ha_mcp.runtime.zone_records import (
        _shape_component_zone_rows,
        _with_registry_icons,
    )

    from ..websocket_api.overview import _do_helpers_list, _helpers_list_prep

    params = {"helper_types": ["zone"], "include_flow_helpers": False}
    extra = await _helpers_list_prep(hass, params)
    result = _do_helpers_list(hass, params, **extra)
    if "zone" not in result["covered_types"]:
        raise UnsupportedOperation("zones.snapshot")
    rows = _shape_component_zone_rows(result)
    entries = [
        entry.as_partial_dict
        for entry in er.async_get(hass).entities.values()
        if entry.platform == "zone" and entry.unique_id
    ]
    return {"rows": _with_registry_icons(rows, entries), "gaps": [], "warnings": []}


async def _scene_inventory(hass: HomeAssistant, arguments: dict[str, Any]) -> Any:
    from homeassistant.helpers import entity_registry as er

    from ha_mcp.runtime.scene_discovery import build_scene_inventory

    from ..websocket_api.search import _do_search, _search_prep

    params: dict[str, Any] = {
        "search_types": ["entity"],
        "domain_filter": "scene",
        "include_hidden": True,
        "limit": 500,
        "offset": 0,
    }
    records: dict[str, dict[str, Any]] = {}
    while True:
        extra = await _search_prep(hass, params)
        result = _do_search(hass, params, **extra)
        if result.get("partial") or result.get("diagnostics"):
            raise ToolFailure(
                "READ_FAILED", "Core returned an incomplete scene inventory"
            )
        page = result["entities"]
        before = len(records)
        records.update((row["entity_id"], row) for row in page)
        if not result["entity_has_more"]:
            break
        if len(records) == before:
            raise ToolFailure(
                "READ_FAILED", "Scene inventory pagination made no progress"
            )
        params["offset"] += len(page)
        await asyncio.sleep(0)
    registry = er.async_get(hass)
    entries = {
        entity_id: entry.as_partial_dict
        for entity_id in records
        if (entry := registry.async_get(entity_id)) is not None
    }
    return build_scene_inventory(list(records.values()), entries)


DIRECT_OPERATIONS: dict[
    str, Callable[[HomeAssistant, dict[str, Any]], Awaitable[Any]]
] = {
    "config.automation.read": _automation_read,
    "config.script.read": _script_read,
    "config.scene.read": _scene_read,
    "config.scene.resolve": _scene_resolve,
    "config.entity_id": _entity_id,
    "entities.category.read": _category,
    "zones.snapshot": _zones,
    "scenes.inventory": _scene_inventory,
}
