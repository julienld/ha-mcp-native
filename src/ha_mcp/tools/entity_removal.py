"""ha_remove_entity's registry removal and its note on a storage helper's entity.

Removing the registry entry of a stored helper's entity leaves the stored item
in place: Core adds its entity again when Home Assistant restarts, and until
then the helper has no entity. The removal says so and names the tool that
removes the helper itself. A YAML helper has a registry entry too but no stored
item, so it gets no note.
"""

from __future__ import annotations

from typing import Any

from ..client.rest_client import HomeAssistantError
from ..runtime.entity_removal_values import (
    storage_note_unread as _unread,
)
from ..runtime.entity_removal_values import (
    stored_helper_removal_note,
)
from .config_helpers.listing import listed_items
from .config_helpers.schemas import SIMPLE_HELPER_TYPES


async def _stored_helper_note(client: Any, entity_id: str) -> list[str]:
    """The note for the entity of a stored helper, or why it is unknown."""
    try:
        entry = await client.send_websocket_message(
            {"type": "config/entity_registry/get", "entity_id": entity_id}
        )
        if not entry.get("success"):
            # A missing entry is the removal's own not-found error.
            if entry.get("error_code") == "not_found":
                return []
            return _unread(entity_id, entry.get("error", "registry read failed"))
        result = entry.get("result") or {}
        platform, storage_id = result.get("platform"), result.get("unique_id")
        if platform not in SIMPLE_HELPER_TYPES or not storage_id:
            return []
        listed = await client.send_websocket_message({"type": f"{platform}/list"})
    except HomeAssistantError as err:
        return _unread(entity_id, err)
    if not listed.get("success"):
        return _unread(entity_id, listed.get("error", f"{platform}/list failed"))
    if not any(
        str(item.get("id")) == storage_id for item in listed_items(listed["result"])
    ):
        return []
    return stored_helper_removal_note(entity_id, platform, storage_id)


async def remove_registry_entry(
    client: Any, entity_id: str
) -> tuple[dict[str, Any], list[str]]:
    """Core's reply to removing ``entity_id``'s registry entry, and the notes
    for a stored helper's entity, read before the entry is gone."""
    notes = await _stored_helper_note(client, entity_id)
    result = await client.send_websocket_message(
        {"type": "config/entity_registry/remove", "entity_id": entity_id}
    )
    return result, notes
