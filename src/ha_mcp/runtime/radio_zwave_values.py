"""Pure Z-Wave response projection and inclusion policy shared across transports."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure
from .radio_values import ok

_NODE_LIMIT = 50
_ADD_CREDENTIALS = (
    "qr_provisioning_information",
    "qr_code_string",
    "planned_provisioning_entry",
    "dsk",
)


def diagnostics(node: dict[str, Any]) -> dict[str, Any]:
    node_status = {
        "node_id": node.get("node_id"),
        "status": node.get("status"),
        "is_routing": node.get("is_routing"),
        "is_secure": node.get("is_secure"),
        "highest_security_class": node.get("highest_security_class"),
        "zwave_plus_version": node.get("zwave_plus_version"),
        "is_controller_node": node.get("is_controller_node"),
    }
    return ok("zwave", "diagnostics", node_status=node_status)


def network_status(entry_id: str, net: dict[str, Any]) -> dict[str, Any]:
    controller = net.get("controller", {}) or {}
    nodes = controller.get("nodes", []) or []
    # Drop the (potentially huge) embedded node list from the controller
    # view; the capped per-node summaries below carry the node detail.
    controller_view = {k: v for k, v in controller.items() if k != "nodes"}
    node_summaries = [
        {
            "node_id": n.get("node_id"),
            "status": n.get("status"),
            "is_routing": n.get("is_routing"),
            "is_secure": n.get("is_secure"),
            "zwave_plus_version": n.get("zwave_plus_version"),
            "is_controller_node": n.get("is_controller_node"),
        }
        for n in nodes[:_NODE_LIMIT]
    ]
    out = ok(
        "zwave",
        "network_status",
        config_entry_id=entry_id,
        controller=controller_view,
        nodes=node_summaries,
        count=len(node_summaries),
        total_count=len(nodes),
    )
    if len(nodes) > _NODE_LIMIT:
        out["truncated"] = True
    return out


def validate_inclusion(args: dict[str, Any]) -> None:
    provided = [c for c in _ADD_CREDENTIALS if args.get(c)]
    if not provided:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "zwave/add requires one of "
                + ", ".join(_ADD_CREDENTIALS)
                + " (or params.smart_start=True). Interactive S2 "
                "read-the-PIN pairing is not scriptable — use the Home "
                "Assistant UI.",
                context={"radio": "zwave", "action": "add"},
                suggestions=[
                    "Pass qr_provisioning_information for a scanned device",
                    "Set params.smart_start=True to add to the SmartStart list",
                    "Use the Home Assistant UI for interactive S2 PIN pairing",
                ],
            )
        )
    if len(provided) > 1:
        # HA's add_node schema marks these as vol.Exclusive — passing more
        # than one is rejected upstream, so fail fast with a clear conflict.
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "zwave/add accepts only one inclusion credential, but "
                + str(len(provided))
                + " were provided: "
                + ", ".join(provided)
                + ". "
                + ", ".join(_ADD_CREDENTIALS)
                + " are mutually exclusive.",
                context={"radio": "zwave", "action": "add", "conflict": provided},
                suggestions=[
                    "Pass exactly one of " + ", ".join(_ADD_CREDENTIALS),
                ],
            )
        )
