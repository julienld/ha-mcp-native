"""Core registry metadata used by integration identity and confirmation guards."""

from typing import Any

from homeassistant.core import HomeAssistant


async def identity_entities(hass: HomeAssistant, arguments: dict[str, Any]) -> Any:
    from homeassistant.helpers import entity_registry as er

    return [
        row.as_partial_dict
        for row in er.async_entries_for_config_entry(
            er.async_get(hass), arguments["entry_id"]
        )
    ]


async def identity_devices(hass: HomeAssistant, arguments: dict[str, Any]) -> Any:
    from ..websocket_api.lookups import _do_device_list

    return _do_device_list(hass, {})["devices"]


async def identity_unique_id(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> dict[str, Any]:
    entry = hass.config_entries.async_get_entry(arguments["entry_id"])
    if entry is None or (
        entry.unique_id is not None and not isinstance(entry.unique_id, str)
    ):
        return {"known": False, "value": None}
    return {"known": True, "value": entry.unique_id}


async def domain_unique_ids(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> dict[str, str] | None:
    result = {}
    for entry in hass.config_entries.async_entries(arguments["domain"]):
        if entry.unique_id is None:
            continue
        if not isinstance(entry.unique_id, str):
            return None
        result[entry.entry_id] = entry.unique_id
    return result


async def list_entries(hass: HomeAssistant, arguments: dict[str, Any]) -> Any:
    from homeassistant.components.config.config_entries import (
        _async_matching_config_entries_json_fragments,
    )

    return await _async_matching_config_entries_json_fragments(hass, None, None)


DIRECT_OPERATIONS = {
    "integrations.identity.entities": (identity_entities, False),
    "integrations.identity.devices": (identity_devices, False),
    "integrations.identity.unique_id": (identity_unique_id, False),
    "integrations.identity.domain_unique_ids": (domain_unique_ids, False),
    "config.entries.list": (list_entries, False),
}
