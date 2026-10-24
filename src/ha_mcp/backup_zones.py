"""The zone a zone tool names, its registry icon, and a removed zone on restore.

The helper tools keep a zone's icon in the entity registry unless the stored
zone already has one (#2643), so the stored item alone can miss it: the
snapshot records the registry's icon too. Core's ``zone/update`` needs the
stored zone; once it was removed, the restore creates it again, and Core takes
the new zone's id from the slug of its name. Core keeps a removed zone's
registry entry for 30 days, so a zone created again under its old id in that
window gets back its entity_id (if still free), registry name, area, labels
and icon; otherwise, or under another id (the zone was renamed since it was
created, or its slug was taken), it gets a new entry. Either way the recorded
registry icon is written on top.
"""

from __future__ import annotations

from typing import Any

from .backup_entity_ids import _manager, with_registry_icon
from .backup_helper_target import stored_target


async def fetch_zone(client: Any, zone_id: str) -> Any:
    """The snapshot of the zone ``ha_set_zone`` or ``ha_remove_zone`` changes,
    resolved by ``stored_target``: a bare id is a storage id, and only
    ``zone.<name>`` is the entity_id of a zone's registry entry, which also
    finds a renamed zone."""
    bm = _manager()
    zones = bm._require_list(
        await bm._ws_send(client, {"type": "zone/list"}), "zone/list"
    )
    stored = {str(zone.get("id")): zone for zone in zones}
    storage_id, _ = stored_target(
        "zone", zone_id, stored, await bm._entity_registry_rows(client)
    )
    zone = stored.get(storage_id) if storage_id else None
    return await with_registry_icon(client, "zone", zone) if zone is not None else None


async def _recreate(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from .backends.remote_snapshot_storage import RemoteStoredRestorePorts
    from .runtime.snapshot_storage_restore import recreate_zone

    return await recreate_zone(
        RemoteStoredRestorePorts(client, "zone"),
        {
            key: value
            for key, value in payload.items()
            if key not in ("type", "zone_id")
        },
    )


async def restore_zone(client: Any, zone_id: str, payload: dict[str, Any]) -> Any:
    from .backends.remote_snapshot_storage import RemoteStoredRestorePorts
    from .runtime.snapshot_storage_restore import restore_zone as restore

    return await restore(
        RemoteStoredRestorePorts(client, "zone"),
        zone_id,
        {
            key: value
            for key, value in payload.items()
            if key not in ("type", "zone_id")
        },
    )
