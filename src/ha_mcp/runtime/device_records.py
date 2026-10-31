"""Shared pure device-registry projections, filtering and pagination."""

from typing import Any, Literal

from ha_mcp.utils.device_registry_semantics import EFFECTIVE_AREA_MARKER

from .projection import build_pagination_metadata


def _process_device_domain(
    domain: str,
    value: str,
    ieee_address: str | None,
    is_z2m: bool,
    zwave_node_id: str | None,
) -> tuple[str | None, bool, str | None]:
    """Return updated (ieee_address, is_z2m, zwave_node_id) for a single identifier domain."""
    # ZHA: identifier is ["zha", "IEEE_ADDRESS"]
    if domain == "zha":
        return value, is_z2m, zwave_node_id
    # Z2M: identifier is ["mqtt", "zigbee2mqtt_0xIEEE"] or "zigbee2mqtt_bridge_0xIEEE"
    if domain == "mqtt" and "zigbee2mqtt" in value.lower():
        extracted = (
            "0x" + value.rsplit("_0x", maxsplit=1)[-1]
            if "_0x" in value
            else ieee_address
        )
        return extracted, True, zwave_node_id
    # Z-Wave JS: identifier is ["zwave_js", "{home_id}-{node_id}"]
    if domain == "zwave_js" and "-" in value:
        return ieee_address, is_z2m, value.split("-")[1]
    return ieee_address, is_z2m, zwave_node_id


def _extract_zigbee_info(
    identifiers: list[Any],
) -> tuple[list[str], str | None, bool, str | None]:
    """Parse device identifiers into (integration_sources, ieee_address, is_z2m, zwave_node_id)."""
    integration_sources: list[str] = []
    ieee_address: str | None = None
    is_z2m = False
    zwave_node_id: str | None = None

    for identifier in identifiers:
        if isinstance(identifier, (list, tuple)) and len(identifier) >= 2:
            domain = identifier[0]
            if isinstance(domain, str):
                if domain not in integration_sources:
                    integration_sources.append(domain)
                ieee_address, is_z2m, zwave_node_id = _process_device_domain(
                    domain,
                    str(identifier[1]),
                    ieee_address,
                    is_z2m,
                    zwave_node_id,
                )

    return integration_sources, ieee_address, is_z2m, zwave_node_id


def _first_ieee_from_connections(
    connections: list[Any], existing_ieee: str | None
) -> str | None:
    """Return existing_ieee if already set; otherwise the first 'ieee' connection value, or None."""
    if not isinstance(connections, list) or existing_ieee:
        return existing_ieee
    for connection in connections:
        if (
            isinstance(connection, (list, tuple))
            and len(connection) >= 2
            and connection[0] == "ieee"
        ):
            return str(connection[1])
    return existing_ieee


def _classify_integration(integration_sources: list[str], is_z2m: bool) -> str:
    """Return the primary integration type string for a device."""
    if "zha" in integration_sources:
        return "zha"
    if is_z2m:
        return "zigbee2mqtt"
    if "zwave_js" in integration_sources:
        return "zwave_js"
    if "mqtt" in integration_sources:
        return "mqtt"
    if integration_sources:
        return integration_sources[0]
    return "unknown"


def _get_device_info(device: dict[str, Any]) -> dict[str, Any]:
    """Extract integration info and build a device summary dict."""
    integration_sources, ieee_address, is_z2m, zwave_node_id = _extract_zigbee_info(
        device.get("identifiers", [])
    )
    ieee_address = _first_ieee_from_connections(
        device.get("connections", []), ieee_address
    )
    integration_type = _classify_integration(integration_sources, is_z2m)
    friendly_name = device.get("name_by_user") or device.get("name")

    device_info: dict[str, Any] = {
        "device_id": device.get("id"),
        "name": friendly_name,
        "manufacturer": device.get("manufacturer"),
        "model": device.get("model"),
        "sw_version": device.get("sw_version"),
        "area_id": device.get(EFFECTIVE_AREA_MARKER, device.get("area_id")),
        "integration_type": integration_type,
        "integration_sources": integration_sources,
        "via_device_id": device.get("via_device_id"),
    }

    if ieee_address:
        device_info["ieee_address"] = ieee_address
    if integration_type == "zigbee2mqtt":
        device_info["friendly_name"] = friendly_name
        device_info["mqtt_topic_hint"] = f"zigbee2mqtt/{friendly_name}/..."
    if integration_type == "zha" and ieee_address:
        device_info["zha_trigger_hint"] = (
            f"Use ieee '{ieee_address}' for zha_event triggers"
        )
    if integration_type == "zwave_js" and zwave_node_id:
        device_info["node_id"] = zwave_node_id

    return device_info


def _device_config_entries(device: dict[str, Any]) -> list[str]:
    """Return the exact owning config-entry IDs for a main or child row.

    Ordinary ``DeviceEntry`` rows carry ``config_entries`` while Core 2026.9
    ``ChildDeviceEntry`` rows carry one singular ``config_entry_id``. Never mix,
    coerce, or repair malformed ownership evidence: an explicitly present plural
    field is authoritative and must be a list of non-empty strings.
    """
    if "config_entries" in device:
        raw = device.get("config_entries")
        if not isinstance(raw, list) or not all(
            isinstance(entry_id, str) and entry_id for entry_id in raw
        ):
            return []
        return list(raw)
    entry_id = device.get("config_entry_id")
    return [entry_id] if isinstance(entry_id, str) and entry_id else []


def _build_entity_maps(
    all_entities: list[dict[str, Any]], need_full: bool
) -> tuple[dict[str, str], dict[str, list[dict[str, Any]]]]:
    """Build entity→device and device→entities lookup maps.

    device_to_entities is only populated when need_full is True; building it for
    every list-mode request would be wasteful when only entity→device lookup is needed.
    """
    entity_to_device: dict[str, str] = {}
    device_to_entities: dict[str, list[dict[str, Any]]] = {}
    for e in all_entities:
        eid = e.get("entity_id")
        did = e.get("device_id")
        if eid and did:
            entity_to_device[eid] = did
            if need_full:
                device_to_entities.setdefault(did, []).append(
                    {
                        "entity_id": eid,
                        "name": e.get("name") or e.get("original_name"),
                        "platform": e.get("platform"),
                    }
                )
    return entity_to_device, device_to_entities


def _matches_integration(
    device_info: dict[str, Any], integration_lower: str, named_types: list[str]
) -> bool:
    """Check whether a device matches the requested integration filter."""
    if integration_lower in named_types:
        return bool(device_info.get("integration_type") == integration_lower)
    return integration_lower in device_info.get("integration_sources", [])


def _filter_devices(
    all_devices: list[dict[str, Any]],
    area_id: str | None,
    manufacturer_lower: str | None,
    integration_lower: str | None,
    device_to_entities: dict[str, list[dict[str, Any]]],
    detail_level: Literal["summary", "full"],
) -> list[dict[str, Any]]:
    """Filter devices for list mode and optionally attach entity lists."""
    named_types = ["zigbee2mqtt", "zha", "zwave_js"]
    matched: list[dict[str, Any]] = []
    for device in all_devices:
        effective_area = device.get(EFFECTIVE_AREA_MARKER, device.get("area_id"))
        if area_id and effective_area != area_id:
            continue
        device_man = (device.get("manufacturer") or "").lower()
        if manufacturer_lower and manufacturer_lower not in device_man:
            continue
        device_info = _get_device_info(device)
        if integration_lower and not _matches_integration(
            device_info, integration_lower, named_types
        ):
            continue
        if detail_level == "full":
            device_info["entities"] = device_to_entities.get(device.get("id") or "", [])
        matched.append(device_info)
    return matched


def _add_integration_hints(
    result: dict[str, Any],
    integration_lower: str | None,
    matched_devices: list[dict[str, Any]],
) -> None:
    """Add Z2M bridge info and integration-specific usage hints to result in-place."""
    if integration_lower == "zigbee2mqtt":
        bridge_info = None
        for d in matched_devices:
            if (
                d.get("via_device_id") is None
                and "bridge" in (d.get("name") or "").lower()
            ):
                bridge_info = {
                    "device_id": d.get("device_id"),
                    "name": d.get("name"),
                    "ieee_address": d.get("ieee_address"),
                }
                break
        if bridge_info:
            result["bridge"] = bridge_info
        result["usage_hint"] = (
            "Use 'friendly_name' for MQTT topics: zigbee2mqtt/{friendly_name}/action"
        )
    elif integration_lower == "zha":
        result["usage_hint"] = (
            "Use 'ieee_address' for zha_event triggers in automations"
        )
    elif integration_lower == "zwave_js":
        result["usage_hint"] = (
            "Use node_id for Z-Wave device identification. "
            "Single device lookup includes node status (security, routing)."
        )


def _list_devices_result(
    all_devices: list[dict[str, Any]],
    device_to_entities: dict[str, list[dict[str, Any]]],
    integration: str | None,
    area_id: str | None,
    manufacturer: str | None,
    limit: int,
    offset: int,
    detail_level: Literal["summary", "full"],
) -> dict[str, Any]:
    """Build the paginated device list response."""
    integration_lower = integration.lower() if integration else None
    manufacturer_lower = manufacturer.lower() if manufacturer else None

    matched_devices = _filter_devices(
        all_devices,
        area_id,
        manufacturer_lower,
        integration_lower,
        device_to_entities,
        detail_level,
    )
    total_matched = len(matched_devices)
    paginated = matched_devices[offset : offset + limit]

    result: dict[str, Any] = {
        "success": True,
        **build_pagination_metadata(total_matched, offset, limit, len(paginated)),
        "total_devices": len(all_devices),
        "devices": paginated,
        "detail_level": detail_level,
    }

    filters_applied = []
    if integration:
        result["integration_filter"] = integration
        filters_applied.append(f"integration={integration}")
    if area_id:
        result["area_filter"] = area_id
        filters_applied.append(f"area_id={area_id}")
    if manufacturer:
        result["manufacturer_filter"] = manufacturer
        filters_applied.append(f"manufacturer={manufacturer}")
    if filters_applied:
        result["filters"] = filters_applied

    _add_integration_hints(result, integration_lower, matched_devices)
    return result
