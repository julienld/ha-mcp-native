"""Zone recreation and tag metadata semantics shared by both backends."""

from typing import Any, Protocol

from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .snapshot_payloads import _strip_readonly


class StoredZoneMissing(Exception):
    """The update target is authoritatively absent, permitting zone recreation."""


class StoredRestorePorts(Protocol):
    async def update(self, item_id: str, fields: dict[str, Any]) -> dict[str, Any]: ...
    async def create(self, fields: dict[str, Any]) -> dict[str, Any]: ...
    async def items(self) -> list[dict[str, Any]]: ...
    async def entity(self, item_id: str) -> dict[str, Any] | None: ...
    async def clear_name(self, entity: dict[str, Any]) -> None: ...
    async def icon(self, item_id: str, value: str | None) -> list[str]: ...


async def recreate_zone(
    ports: StoredRestorePorts, fields: dict[str, Any]
) -> dict[str, Any]:
    """Reuse an exact earlier recreation without claiming an identical zone's metadata."""
    zones = await ports.items()
    restored = next(
        (
            zone
            for zone in zones
            if json_values_equal(_strip_readonly(zone, "id"), fields)
        ),
        None,
    )
    if restored is not None:
        return {**restored, "restore_mode": "already_recreated"}
    return {**await ports.create(fields), "restore_mode": "recreated"}


async def restore_zone(
    ports: StoredRestorePorts, zone_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    recorded = "registry_icon" in payload
    fields = dict(payload)
    registry_icon = fields.pop("registry_icon", None)
    try:
        result = await ports.update(zone_id, fields)
    except StoredZoneMissing:
        result = await recreate_zone(ports, fields)
        zone_id = result.get("id", zone_id)
    if result.get("restore_mode") == "already_recreated":
        warning = (
            f"Zone {zone_id} matches every stored field of the snapshot, so it was "
            "taken for the zone an earlier restore of this snapshot created and "
            "left unchanged, its registry icon included. Core allows identical "
            f"zones: if {zone_id} is a different zone, the removed zone was not "
            "created again."
        )
        if registry_icon is not None:
            warning += (
                f" The snapshot's registry icon is {registry_icon}; set it with "
                "ha_set_entity if the earlier restore could not."
            )
        return {**result, "warnings": [warning]}
    warnings: list[str] = []
    if result.get("icon") != fields.get("icon"):
        warnings.append(
            f"The zone keeps its stored icon {result.get('icon')}: Core's zone "
            "update cannot remove an icon stored after the snapshot."
        )
    if recorded:
        warnings += await ports.icon(zone_id, registry_icon)
    return {**result, "warnings": warnings} if warnings else result


async def restore_tag(
    ports: StoredRestorePorts, tag_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Keep an originally unnamed tag unnamed, without submitting an invalid name."""
    fields = dict(payload)
    recorded = "registry_icon" in fields
    registry_icon = fields.pop("registry_icon", None)
    unnamed = fields.get("name") is None
    if unnamed:
        fields.pop("name", None)
    result = await ports.update(tag_id, fields)
    entity = await ports.entity(tag_id) if unnamed else None
    if entity and entity.get("name") is not None:
        async with registry_update_lock("entity", entity["entity_id"]):
            await ports.clear_name(entity)
    if not recorded:
        return result
    warnings = await ports.icon(tag_id, registry_icon)
    return {**result, "warnings": warnings} if warnings else result
