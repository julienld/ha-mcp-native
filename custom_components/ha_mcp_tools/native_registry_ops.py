"""Native registry operations reuse the existing owner/validator component seam."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.core import HomeAssistant

from ha_mcp.tool_api import ToolFailure

# This is a semantic capability map, never permission to execute arbitrary WS
# handlers. NativeBackend applies the schema and calls the do/prep owners.
COMMAND_OPERATIONS: dict[str, tuple[str, bool]] = {
    "registries.snapshot": ("ha_mcp_tools/registries", False),
    "assist.exposure": ("ha_mcp_tools/exposure", False),
    "entities.enrich": ("ha_mcp_tools/entity_enrich", False),
}


def _require_loaded(hass: HomeAssistant, domain: str) -> None:
    if domain not in hass.config.components:
        raise ToolFailure("RADIO_UNAVAILABLE", f"{domain} integration is not loaded")


async def _zha_devices(hass: HomeAssistant, arguments: dict[str, Any]) -> Any:
    _require_loaded(hass, "zha")
    from homeassistant.components.zha.helpers import get_zha_gateway_proxy

    gateway = get_zha_gateway_proxy(hass)
    return [device.zha_device_info for device in gateway.device_proxies.values()]


async def _zwave_node_status(hass: HomeAssistant, arguments: dict[str, Any]) -> Any:
    _require_loaded(hass, "zwave_js")
    from homeassistant.components.zwave_js.api import node_status
    from homeassistant.components.zwave_js.helpers import async_get_node_from_device_id

    try:
        node = async_get_node_from_device_id(hass, arguments["device_id"])
    except ValueError as error:
        raise ToolFailure("RADIO_UNAVAILABLE", str(error)) from error
    return node_status(node)


async def _matter_node_diagnostics(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> Any:
    _require_loaded(hass, "matter")
    from homeassistant.components.matter.helpers import (
        MissingNode,
        get_matter,
        node_from_ha_device_id,
    )
    from matter_server.client.exceptions import ServerVersionTooOld
    from matter_server.common.errors import MatterError
    from matter_server.common.helpers.util import dataclass_to_dict

    try:
        matter = get_matter(hass)
        node = node_from_ha_device_id(hass, arguments["device_id"])
        if node is None:
            raise ToolFailure(
                "RADIO_UNAVAILABLE",
                f"Could not resolve Matter node from device id {arguments['device_id']}",
            )
        result = await matter.matter_client.node_diagnostics(node_id=node.node_id)
    except (MatterError, MissingNode, ServerVersionTooOld) as error:
        raise ToolFailure("RADIO_UNAVAILABLE", str(error)) from error
    return dataclass_to_dict(result)


def _entity_rows(hass: HomeAssistant) -> list[dict[str, Any]]:
    from homeassistant.helpers import entity_registry as er

    return [entry.as_partial_dict for entry in er.async_get(hass).entities.values()]


async def _device_resolve(hass: HomeAssistant, arguments: dict[str, Any]) -> Any:
    from homeassistant.helpers import config_validation as cv
    from homeassistant.helpers import entity_registry as er

    from .helper_collections import _INVALID_ERRORS

    try:
        entity_id = cv.entity_id(arguments["entity_id"])
    except _INVALID_ERRORS:
        return None
    entry = er.async_get(hass).async_get(entity_id)
    return entry.device_id if entry else None


async def _entity_list(hass: HomeAssistant, arguments: dict[str, Any]) -> Any:
    return _entity_rows(hass)


async def _entity_read(hass: HomeAssistant, arguments: dict[str, Any]) -> Any:
    from homeassistant.helpers import config_validation as cv
    from homeassistant.helpers import entity_registry as er

    from .helper_collections import _INVALID_ERRORS

    registry = er.async_get(hass)
    ids = arguments["entity_ids"]
    entries: dict[str, Any] = {}
    errors: dict[str, str] = {}
    single = arguments.get("single", False)
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        try:
            normalized = [cv.entity_id(chunk[0])] if single else cv.entity_ids(chunk)
        except _INVALID_ERRORS as error:
            errors.update(dict.fromkeys(chunk, str(error)))
            continue
        result = {
            entity_id: entry.extended_dict
            if (entry := registry.async_get(entity_id))
            else None
            for entity_id in normalized
        }
        for entity_id in chunk:
            row = result.get(normalized[0] if single else entity_id)
            if row is None:
                errors[entity_id] = "Entity not found"
            else:
                entries[entity_id] = row
        await asyncio.sleep(0)
    return {"entries": entries, "errors": errors}


async def _device_single_snapshot(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> Any:
    from ha_mcp.runtime.device_records import _build_entity_maps
    from ha_mcp.utils.device_registry_semantics import (
        EFFECTIVE_AREA_MARKER,
        annotate_device_rows_with_effective_area,
    )

    from .websocket_api.lookups import _do_device_get, _do_device_list

    device_id = arguments["device_id"]
    result = _do_device_get(hass, {"device_id": device_id, "include_entities": True})
    device, entities = result.get("device"), result.get("entities")
    if isinstance(device, dict) and isinstance(entities, list):
        device = dict(device)
        if "effective_area_id" in result:
            device[EFFECTIVE_AREA_MARKER] = result["effective_area_id"]
        devices = [device]
    else:
        devices = annotate_device_rows_with_effective_area(
            _do_device_list(hass, {})["devices"]
        )
        entities = _entity_rows(hass)
    _, entity_map = _build_entity_maps(entities, True)
    return {"devices": devices, "device_to_entities": entity_map}


async def _device_list_snapshot(hass: HomeAssistant, arguments: dict[str, Any]) -> Any:
    from ha_mcp.runtime.device_records import _build_entity_maps
    from ha_mcp.utils.device_registry_semantics import (
        annotate_device_rows_with_effective_area,
    )

    from .websocket_api.lookups import _do_device_list

    need_full = arguments["include_entities"]
    entities = _entity_rows(hass) if need_full else []
    _, entity_map = _build_entity_maps(entities, need_full)
    devices = annotate_device_rows_with_effective_area(
        _do_device_list(hass, {})["devices"]
    )
    return {"devices": devices, "device_to_entities": entity_map}


DIRECT_OPERATIONS: dict[
    str, Callable[[HomeAssistant, dict[str, Any]], Awaitable[Any]]
] = {
    "radio.zha.devices": _zha_devices,
    "radio.zwave.node_status": _zwave_node_status,
    "radio.matter.node_diagnostics": _matter_node_diagnostics,
    "devices.resolve": _device_resolve,
    "devices.single_snapshot": _device_single_snapshot,
    "devices.list_snapshot": _device_list_snapshot,
    "entities.registry.read": _entity_read,
    "entities.registry.list": _entity_list,
}
