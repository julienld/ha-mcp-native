"""Give a recreated flow helper's entities their saved IDs and names.

Split out of ``backup_manager`` (#2632), which imports this module; the manager's
own helpers are reached lazily so its tests can patch them in place.
"""

from __future__ import annotations

from typing import Any

from ._vendor.websockets.exceptions import WebSocketException
from .utils.registry_update_lock import registry_update_lock


def _manager() -> Any:
    from . import backup_manager

    return backup_manager


async def registry_row(
    client: Any, platform: str, unique_id: str
) -> dict[str, Any] | None:
    """The entity-registry row of ``platform``'s entity with ``unique_id``."""
    rows = await _manager()._entity_registry_rows(client)
    return next(
        (
            row
            for row in rows
            if row.get("platform") == platform and row.get("unique_id") == unique_id
        ),
        None,
    )


async def with_registry_icon(
    client: Any, platform: str, item: dict[str, Any]
) -> dict[str, Any]:
    """The stored item plus the icon its entity's registry entry sets, if any.

    The registry icon is the one the entity shows, and the helper tools write
    it there (an update always; a create for a zone, or when Core's schema
    has no icon field or cannot be read), so the stored item alone can miss
    it. A failed registry read
    propagates, so the capture reports it instead of saving a snapshot
    without the icon.
    """
    entity = await registry_row(client, platform, str(item.get("id")))
    return {**item, "registry_icon": entity.get("icon") if entity else None}


async def restore_registry_icon(
    client: Any, platform: str, item_id: str, registry_icon: str | None
) -> list[str]:
    """Write the recorded registry icon back; problems come back as warnings,
    because the stored item was already restored."""
    bm = _manager()
    try:
        entity = await registry_row(client, platform, item_id)
        if entity is None:
            if registry_icon is None:
                return []
            return [
                f"The registry icon {registry_icon} was not restored: "
                f"{platform} {item_id} has no entity."
            ]
        async with registry_update_lock("entity", entity["entity_id"]):
            await bm._ws_send(
                client,
                {
                    "type": "config/entity_registry/update",
                    "entity_id": entity["entity_id"],
                    "icon": registry_icon,
                },
            )
    except (*bm._CAPTURE_TRANSIENT_ERRORS, WebSocketException) as err:
        return [f"The {platform} was restored, but its registry icon was not: {err}"]
    return []


async def update_with_registry_icon(
    client: Any, platform: str, item_id: str, payload: dict[str, Any]
) -> Any:
    """``<platform>/update`` without the snapshot's registry icon, then the icon."""
    recorded = "registry_icon" in payload
    registry_icon = payload.pop("registry_icon", None)
    result = await _manager()._ws_send(client, payload)
    warnings = (
        await restore_registry_icon(client, platform, item_id, registry_icon)
        if recorded
        else []
    )
    return {**result, "warnings": warnings} if warnings else result


async def _restore_entity_ids(
    client: Any, entry_id: str, original_entry_id: str, saved_rows: list[dict[str, Any]]
) -> list[dict[str, str]]:
    from .backends.remote_snapshot_flows import RemoteSnapshotFlowPorts
    from .runtime.snapshot_entity_restore import _restore_entity_ids as restore

    return await restore(
        RemoteSnapshotFlowPorts(client), entry_id, original_entry_id, saved_rows
    )


__all__ = [
    "registry_row",
    "restore_registry_icon",
    "update_with_registry_icon",
    "with_registry_icon",
]
