"""Best-effort device enrichment through narrow native/remote radio operations."""

from typing import Any

from ha_mcp.tool_api import Backend, CallContext, ToolFailure


async def _optional_radio(
    backend: Backend,
    context: CallContext,
    operation: str,
    arguments: dict[str, Any],
    warnings: list[str],
    label: str,
) -> tuple[bool, Any]:
    try:
        return True, await backend.execute(operation, arguments, context)
    except (ToolFailure, TimeoutError, OSError) as error:
        if isinstance(error, ToolFailure) and error.code not in {
            "RADIO_UNAVAILABLE",
            "UNSUPPORTED_OPERATION",
        }:
            raise
        warnings.append(f"{label} unavailable: {error}")
        return False, None


async def enrich_zha_metrics(
    backend: Backend,
    context: CallContext,
    device_info: dict[str, Any],
    warnings: list[str],
) -> None:
    available, devices = await _optional_radio(
        backend, context, "radio.zha.devices", {}, warnings, "ZHA radio metrics"
    )
    if available:
        by_ieee = {
            device.get("ieee"): device for device in devices if device.get("ieee")
        }
        device = by_ieee.get(device_info["ieee_address"])
        if device:
            device_info["radio_metrics"] = {
                "lqi": device.get("lqi"),
                "rssi": device.get("rssi"),
            }


async def enrich_zwave_status(
    backend: Backend,
    context: CallContext,
    device_id: str,
    device_info: dict[str, Any],
    warnings: list[str],
) -> None:
    available, node = await _optional_radio(
        backend,
        context,
        "radio.zwave.node_status",
        {"device_id": device_id},
        warnings,
        "Z-Wave node status",
    )
    if available:
        device_info["node_status"] = {
            "node_id": node.get("node_id"),
            "status": node.get("status"),
            "is_routing": node.get("is_routing"),
            "is_secure": node.get("is_secure"),
            "highest_security_class": node.get("highest_security_class"),
            "zwave_plus_version": node.get("zwave_plus_version"),
            "is_controller_node": node.get("is_controller_node"),
        }


async def enrich_matter_diagnostics(
    backend: Backend,
    context: CallContext,
    device_id: str,
    device_info: dict[str, Any],
    warnings: list[str],
) -> None:
    available, node = await _optional_radio(
        backend,
        context,
        "radio.matter.node_diagnostics",
        {"device_id": device_id},
        warnings,
        "Matter node diagnostics",
    )
    if available:
        device_info["node_diagnostics"] = {
            "network_type": node.get("network_type"),
            "node_type": node.get("node_type"),
            "available": node.get("available"),
            "network_name": node.get("network_name"),
            # Keep the existing public correction of the upstream misspelling.
            "ip_addresses": node.get("ip_adresses"),
            "mac_address": node.get("mac_address"),
            "active_fabrics": node.get("active_fabrics"),
            "active_fabric_index": node.get("active_fabric_index"),
        }
