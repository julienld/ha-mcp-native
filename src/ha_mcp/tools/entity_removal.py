"""ha_remove_entity's registry removal and its note on a storage helper's entity.

Removing the registry entry of a stored helper's entity leaves the stored item
in place: Core adds its entity again when Home Assistant restarts, and until
then the helper has no entity. The removal says so and names the tool that
removes the helper itself. A YAML helper has a registry entry too but no stored
item, so it gets no note.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..client.rest_client import (
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
    HomeAssistantError,
)
from ..errors import ErrorCode
from ..runtime.entity_removal_values import (
    storage_note_unread as _unread,
)
from ..runtime.entity_removal_values import (
    stored_helper_removal_note,
)
from .config_helpers.listing import listed_items
from .config_helpers.schemas import SIMPLE_HELPER_TYPES
from .helpers import exception_to_structured_error


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


async def remove_registry_entries(
    client: Any,
    entity_ids: list[str],
    error_text: Callable[[dict[str, Any]], str],
    *,
    remove: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Remove each id in turn; each lands in ``removed``, ``skipped`` (Core's
    not_found: already absent) or ``errors``. A lost connection stops the run
    and lists the ids it never sent, which are safe to retry: the ones after the
    failing id, and the failing id too when its command provably never left."""
    removed: list[str] = []
    skipped: list[str] = []
    errors: list[dict[str, str]] = []
    not_attempted: list[str] = []
    for index, eid in enumerate(entity_ids):
        try:
            result = (
                await remove(eid)
                if remove is not None
                else await client.send_websocket_message(
                    {"type": "config/entity_registry/remove", "entity_id": eid}
                )
            )
        except HomeAssistantConnectionError as exc:
            # CONNECTION_FAILED, CONNECTION_TIMEOUT for a timed-out command,
            # or AUTH_* when the token was rejected on reconnect.
            structured = exception_to_structured_error(exc, raise_error=False)
            errors.append(
                {
                    "entity_id": eid,
                    "code": structured["error"]["code"],
                    "message": str(exc),
                }
            )
            unsent = isinstance(exc, HomeAssistantCommandNotSent)
            not_attempted = list(entity_ids[index if unsent else index + 1 :])
            break
        if result.get("success"):
            removed.append(eid)
        elif result.get("error_code") == "not_found":
            skipped.append(eid)
        else:
            errors.append(
                {
                    "entity_id": eid,
                    "code": ErrorCode.SERVICE_CALL_FAILED.value,
                    "message": error_text(result),
                }
            )
    response: dict[str, Any] = {
        "success": not errors,
        "total": len(entity_ids),
        "removed": removed,
        "skipped": skipped,
        "errors": errors,
    }
    if not_attempted:
        response["not_attempted"] = not_attempted
    return response
