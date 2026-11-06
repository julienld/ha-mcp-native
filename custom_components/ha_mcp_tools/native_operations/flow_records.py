"""Config entry metadata and helper-flow discovery from their Core owners."""

from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.core import HomeAssistant

from ha_mcp.errors import HomeAssistantAPIError


async def helper_types(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> dict[str, Any]:
    from homeassistant.loader import async_get_config_flows

    return {"types": sorted(await async_get_config_flows(hass, type_filter="helper"))}


async def entry_metadata(hass: HomeAssistant, arguments: dict[str, Any]) -> Any:
    entry = hass.config_entries.async_get_entry(arguments["entry_id"])
    if entry is None:
        raise HomeAssistantAPIError("Config entry not found", status_code=404)
    return entry.as_json_fragment


async def subentries(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> list[dict[str, Any]]:
    entry = hass.config_entries.async_get_entry(arguments["entry_id"])
    if entry is None:
        raise HomeAssistantAPIError("Config entry not found", status_code=404)
    return [
        {
            "subentry_id": row.subentry_id,
            "subentry_type": row.subentry_type,
            "title": row.title,
            "unique_id": row.unique_id,
        }
        for row in entry.subentries.values()
    ]


DIRECT_OPERATIONS: dict[
    str, tuple[Callable[[HomeAssistant, dict[str, Any]], Awaitable[Any]], bool]
] = {
    "helpers.flow.types": (helper_types, False),
    "config.entries.read": (entry_metadata, False),
    "config.subentries.list": (subentries, False),
}
