"""Shared zone snapshots, source classification and public response shaping."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure


def _build_zone_result(
    zones: list[dict[str, Any]], zone_id: str | None, gaps: list[str] | None = None
) -> dict[str, Any]:
    """Assemble the ha_get_zone response from a list of zone records.

    Shared by the legacy ``zone/list`` path and the component ``helpers_list``
    path so the two produce an identical envelope. Without ``zone_id`` returns
    the full list; with one (a zone_id or an entity_id), returns that single
    zone or raises RESOURCE_NOT_FOUND. ``gaps`` names parts of the listing that
    could not be read: they become warnings, and a zone missing from such a
    listing is a read failure, not an absent zone.
    """
    if zone_id is None:
        response: dict[str, Any] = {
            "success": True,
            "count": len(zones),
            "zones": zones,
            "message": f"Found {len(zones)} zone(s)",
        }
        if gaps:
            response["warnings"] = list(gaps)
        return response

    # A stored zone named "Home" has the id "home", like the home zone's row.
    matches = [z for z in zones if zone_id in (z.get("id"), z.get("entity_id"))]
    zone = next((z for z in matches if z.get("editable")), None) or next(
        iter(matches), None
    )
    if zone is None and gaps:
        raise_failure(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Zone {zone_id} is not among the zones that could be read: "
                + "; ".join(gaps),
                context={"zone_id": zone_id},
                suggestions=["Retry once the entity registry and states can be read"],
            )
        )
    if zone is None:
        available_ids = [z.get("id") for z in zones[:10]]  # Show first 10
        raise_failure(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Zone not found: {zone_id}",
                context={
                    "zone_id": zone_id,
                    "available_zone_ids": available_ids,
                },
                suggestions=[
                    "Use ha_get_zone() without zone_id to see all available zones"
                ],
            )
        )
    response = {"success": True, "zone_id": zone_id, "zone": zone}
    if gaps:
        response["warnings"] = list(gaps)
    return response


def _shape_component_zone_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Map one component ``helpers_list`` zone record onto the legacy zone row.

    The component supplies each zone entity's config as ``config``: the stored
    body for a storage zone, and the YAML or core-configuration body for the
    YAML and home zones that ``zone/list`` omits. The row is that body plus
    ``entity_id`` and an ``editable`` / ``source`` discriminator.
    """
    config = rec.get("config")
    out: dict[str, Any] = dict(config) if isinstance(config, dict) else {}
    # The component reads each zone entity's _config, so a zone outside the
    # storage (the home zone, a YAML zone) shows as one without a storage id.
    # Without a _config it falls back to the state attributes and fills the
    # storage id in; Core's editable attribute then marks a YAML zone.
    storage_id = rec.get("storage_id")
    is_yaml = storage_id is None or out.get("editable") is False
    # Storage zones keep their storage id (the key ha_get_zone matches on);
    # YAML zones get their object_id so they can still be fetched by zone_id.
    out["id"] = storage_id if storage_id is not None else rec.get("object_id")
    out["entity_id"] = rec.get("entity_id")
    out["editable"] = not is_yaml
    out["source"] = "yaml" if is_yaml else "storage"
    return out


def _with_registry_icons(
    rows: list[dict[str, Any]], entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The rows with the icon each zone's entity shows: its registry icon, where
    the zone tools keep a new zone's icon (#2643), over the stored one."""
    icons = {e.get("entity_id"): e["icon"] for e in entries if e.get("icon")}
    return [
        {**row, "icon": icons[row["entity_id"]]}
        if row.get("entity_id") in icons
        else row
        for row in rows
    ]


def _shape_component_zone_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Reshape the component's zone ``helpers_list`` result into legacy rows."""
    raw = result.get("helpers")
    records = raw if isinstance(raw, list) else []
    return [
        _shape_component_zone_record(rec)
        for rec in records
        if isinstance(rec, dict) and rec.get("helper_type") == "zone"
    ]


def render_zone_snapshot(
    snapshot: dict[str, Any], zone_id: str | None
) -> dict[str, Any]:
    response = _build_zone_result(snapshot["rows"], zone_id, snapshot.get("gaps"))
    if snapshot.get("warnings"):
        response.setdefault("warnings", []).extend(snapshot["warnings"])
    return response
