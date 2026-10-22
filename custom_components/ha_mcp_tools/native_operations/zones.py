"""Bounded zone collection operations; no transport or registry-delete fallback."""

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry

from ha_mcp.tool_api import UnsupportedOperation

from ..helper_collections import collection_owner
from .entity_waiters import wait_removed
from .helpers import check_expected_item, list_storage_items


async def target_snapshot(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> dict[str, Any]:
    return {
        **list_storage_items(hass, "zone"),
        "entries": [
            {
                "entity_id": entry.entity_id,
                "platform": entry.platform,
                "unique_id": entry.unique_id,
                "icon": entry.icon,
            }
            for entry in entity_registry.async_get(hass).entities.values()
            if entry.platform == "zone" and entry.unique_id
        ],
    }


async def delete_zone(hass: HomeAssistant, arguments: dict[str, Any]) -> dict[str, Any]:
    owner = collection_owner(hass, "zone")
    if owner is None:
        raise UnsupportedOperation("zones.storage.delete")
    # No await separates the compare from collection dispatch. The native
    # backend fences fresh credentials, read-only state and tool policy first.
    check_expected_item(
        hass,
        {"helper_type": "zone", "item_id": arguments["item_id"]},
        arguments["expected_item"],
    )
    await owner.storage_collection.async_delete_item(arguments["item_id"])
    return {"success": True}


DIRECT_OPERATIONS = {
    "zones.target.snapshot": (target_snapshot, False),
    "zones.storage.delete": (delete_zone, True),
    "entities.removal.wait": (wait_removed, False),
}
