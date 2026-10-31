"""Pure entity-registry result contracts shared by native and remote tools."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure


def uses_device_name(entry: dict[str, Any]) -> bool:
    """An empty name override follows the device name; without a device there is nothing to follow."""
    return entry.get("name") == "" and bool(entry.get("device_id"))


def _format_fetched_entity(entry: dict[str, Any]) -> dict[str, Any]:
    """Project a raw HA entity-registry extended dict into ha_get_entity's shape.

    Shared by the single-entity get, the bulk get_entries backend, and the
    unique_id resolver so every ha_get_entity response carries the same keys.
    """
    return {
        "entity_id": entry.get("entity_id"),
        "name": entry.get("name"),
        "uses_device_name": uses_device_name(entry),
        "original_name": entry.get("original_name"),
        "icon": entry.get("icon"),
        "area_id": entry.get("area_id"),
        "disabled_by": entry.get("disabled_by"),
        "hidden_by": entry.get("hidden_by"),
        "enabled": entry.get("disabled_by") is None,
        "hidden": entry.get("hidden_by") is not None,
        "aliases": entry.get("aliases", []),
        "labels": entry.get("labels", []),
        "categories": entry.get("categories", {}),
        "device_class": entry.get("device_class"),
        "original_device_class": entry.get("original_device_class"),
        "options": entry.get("options", {}),
        "platform": entry.get("platform"),
        "device_id": entry.get("device_id"),
        "config_entry_id": entry.get("config_entry_id"),
        "unique_id": entry.get("unique_id"),
    }


def _merge_entity_enrichment(
    record: dict[str, Any], enrichment: dict[str, Any] | None
) -> None:
    """Additively attach resolved area/floor/label NAMES to an entity record.

    The base record already carries ``area_id`` and label *ids* (``labels``); the
    component join adds the resolved, device-inherited NAMES under non-clobbering
    keys — ``area`` / ``floor`` / ``label_names`` — so nothing existing is
    overwritten. ``aliases`` is already on the base record (same set — the
    component sorts, so ordering may differ — and the base value is authoritative),
    so it is not re-added. A ``None`` / empty enrichment is a no-op: on a capability
    miss the fields are simply absent, leaving the legacy response shape unchanged.
    """
    if not enrichment:
        return
    record["area"] = enrichment.get("area")
    record["floor"] = enrichment.get("floor")
    record["label_names"] = enrichment.get("labels") or []


def _match_registry_by_unique_id(
    entries: list[dict[str, Any]],
    unique_id: str,
    domain: str | None,
    platform: str | None,
) -> list[dict[str, Any]]:
    """Return formatted registry entries matching unique_id (+ optional filters).

    ``domain`` is compared against the entity_id prefix (the registry's unique
    key uses the entity domain); ``platform`` against the entry's platform.
    """
    matches: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("unique_id") != unique_id:
            continue
        entry_domain = (entry.get("entity_id") or "").split(".")[0]
        if domain is not None and entry_domain != domain:
            continue
        if platform is not None and entry.get("platform") != platform:
            continue
        matches.append(_format_fetched_entity(entry))
    return matches


def _parse_get_entity_ids(
    entity_id: str | list[str],
) -> tuple[list[str], bool, dict[str, Any] | None]:
    """Parse entity_id into (entity_ids, is_bulk, early_response). early_response is non-None for empty list."""
    if isinstance(entity_id, str):
        return [entity_id], False, None
    if not isinstance(entity_id, list):
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"entity_id must be string or list of strings, got {type(entity_id).__name__}",
            )
        )
    if not entity_id:
        return (
            [],
            False,
            {
                "success": True,
                "entity_entries": [],
                "count": 0,
                "message": "No entities requested",
            },
        )
    if not all(isinstance(e, str) for e in entity_id):
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "All entity_id values must be strings",
            )
        )
    return entity_id, True, None
