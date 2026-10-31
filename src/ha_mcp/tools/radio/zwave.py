"""Z-Wave JS radio handler for ``ha_manage_radio``.

Wraps the Home Assistant ``zwave_js/*`` WebSocket API and the ``zwave_js.ping``
service: per-node diagnostics (read), controller/network summary (read),
non-interactive inclusion / exclusion, re-interview, mesh route rebuilds,
configuration-parameter writes, firmware install, and the network-wiping
controller hard reset.

Configuration-parameter reads use the node's Z-Wave JS values, independently
of enabled Home Assistant entities.

Node-scoped actions key on ``device_id``; controller-scoped actions resolve the
single ``zwave_js`` config entry via ``resolve_entry_id``. Interactive S2 secure
inclusion (the read-the-PIN handshake) is *not* scriptable here — provide
SmartStart/QR/DSK provisioning data, or use the Home Assistant UI.
"""

from __future__ import annotations

from typing import Any

from ha_mcp.backends.legacy import legacy_errors_sync
from ha_mcp.runtime.radio_specs import ZWAVE
from ha_mcp.runtime.radio_zwave_values import diagnostics, network_status
from ha_mcp.runtime.radio_zwave_values import validate_inclusion as _validate_inclusion

from . import zwave_parameters
from .base import (
    ActionSpec,
    integration_not_found,
    integration_required,
    ok,
    require,
    resolve_entry_id,
    resolve_update_entity,
    ws_call,
)

SUPPORTED = ZWAVE


async def _zwave_entry_id(client: Any) -> str | None:
    """Resolve the single ``zwave_js`` config entry_id (None if not configured)."""
    return await resolve_entry_id(client, "zwave_js")


async def handle(client: Any, action: str, args: dict[str, Any]) -> dict[str, Any]:
    """Execute one Z-Wave action (validation/confirm already applied by caller)."""
    device_id = args.get("device_id")

    if action in ("get_config_params", "get_config_param"):
        return await zwave_parameters.read(client, action, args)

    if action == "diagnostics":
        node = (
            await ws_call(
                client,
                "zwave_js/node_status",
                device_id=device_id,
                context={"device_id": device_id},
            )
            or {}
        )
        return diagnostics(node)

    if action == "network_status":
        entry_id = await _zwave_entry_id(client)
        if not entry_id:
            return integration_not_found("zwave", "zwave_js")
        net = (
            await ws_call(
                client,
                "zwave_js/network_status",
                entry_id=entry_id,
                context={"entry_id": entry_id},
            )
            or {}
        )
        return network_status(entry_id, net)

    if action == "ping":
        # zwave_js.ping is a plain service (no response support); it targets the
        # node by device_id and returns the list of affected states.
        result = await client.call_service("zwave_js", "ping", {"device_id": device_id})
        return ok("zwave", "ping", result=result)

    return await _handle_zwave_inclusion(client, action, args, device_id)


async def _handle_zwave_inclusion(
    client: Any, action: str, args: dict[str, Any], device_id: Any
) -> dict[str, Any]:
    """Continuation of :func:`handle` for Z-Wave inclusion/exclusion actions."""
    if action == "add":
        entry_id = await _zwave_entry_id(client)
        if not entry_id:
            integration_required("zwave", "zwave_js")

        if args.get("smart_start"):
            # SmartStart: add the provisioning info to the controller's list; the
            # node joins automatically (and securely) the next time it is powered.
            require(
                args,
                ActionSpec("", required=("qr_provisioning_information",)),
                "zwave",
                "add",
            )
            result = await ws_call(
                client,
                "zwave_js/provision_smart_start_node",
                entry_id=entry_id,
                qr_provisioning_information=args.get("qr_provisioning_information"),
                device_name=args.get("device_name"),
                area_id=args.get("area_id"),
                context={"entry_id": entry_id},
            )
            return ok(
                "zwave", "add", mode="smart_start", result=result, long_running=True
            )

        # Active inclusion. Non-interactive only: require exactly one pre-shared
        # credential so no S2 read-the-PIN handshake is needed.
        validate_inclusion(args)
        result = await ws_call(
            client,
            "zwave_js/add_node",
            entry_id=entry_id,
            inclusion_strategy=args.get("inclusion_strategy"),
            qr_provisioning_information=args.get("qr_provisioning_information"),
            qr_code_string=args.get("qr_code_string"),
            planned_provisioning_entry=args.get("planned_provisioning_entry"),
            dsk=args.get("dsk"),
            context={"entry_id": entry_id},
        )
        return ok("zwave", "add", mode="add_node", result=result, long_running=True)

    if action == "remove_device":
        if args.get("failed"):
            # remove_failed_node targets a specific (already-dead) node.
            require(
                args,
                ActionSpec("", required=("device_id",)),
                "zwave",
                "remove_device",
            )
            result = await ws_call(
                client,
                "zwave_js/remove_failed_node",
                device_id=device_id,
                context={"device_id": device_id},
            )
            return ok("zwave", "remove_device", mode="failed", result=result)

        # Default: open an exclusion window on the controller (long-running; the
        # user triggers exclusion on the physical device to complete it).
        entry_id = await _zwave_entry_id(client)
        if not entry_id:
            integration_required("zwave", "zwave_js")
        result = await ws_call(
            client,
            "zwave_js/remove_node",
            entry_id=entry_id,
            context={"entry_id": entry_id},
        )
        return ok(
            "zwave",
            "remove_device",
            mode="exclusion",
            result=result,
            long_running=True,
        )

    return await _handle_zwave_maintenance(client, action, args, device_id)


async def _handle_zwave_maintenance(
    client: Any, action: str, args: dict[str, Any], device_id: Any
) -> dict[str, Any]:
    """Continuation of :func:`handle` for Z-Wave node maintenance actions."""
    if action == "reinterview":
        result = await ws_call(
            client,
            "zwave_js/refresh_node_info",
            device_id=device_id,
            context={"device_id": device_id},
        )
        return ok("zwave", "reinterview", result=result, long_running=True)

    if action == "rebuild_routes":
        if args.get("scope") == "network":
            entry_id = await _zwave_entry_id(client)
            if not entry_id:
                integration_required("zwave", "zwave_js")
            result = await ws_call(
                client,
                "zwave_js/begin_rebuilding_routes",
                entry_id=entry_id,
                context={"entry_id": entry_id},
            )
            return ok(
                "zwave",
                "rebuild_routes",
                scope="network",
                result=result,
                long_running=True,
            )
        require(
            args,
            ActionSpec("", required=("device_id",)),
            "zwave",
            "rebuild_routes",
        )
        result = await ws_call(
            client,
            "zwave_js/rebuild_node_routes",
            device_id=device_id,
            context={"device_id": device_id},
        )
        return ok(
            "zwave",
            "rebuild_routes",
            scope="node",
            result=result,
            long_running=True,
        )

    if action == "set_config_param":
        result = await ws_call(
            client,
            "zwave_js/set_config_parameter",
            device_id=device_id,
            property=args.get("property"),
            endpoint=args.get("endpoint", 0),
            property_key=args.get("property_key"),
            value=args.get("value"),
            context={"device_id": device_id, "property": args.get("property")},
        )
        return ok("zwave", "set_config_param", result=result)

    if action == "firmware_update":
        update_entity = await resolve_update_entity(
            client, device_id, platform="zwave_js"
        )
        await client.call_service("update", "install", {"entity_id": update_entity})
        return ok(
            "zwave", "firmware_update", entity_id=update_entity, long_running=True
        )

    if action == "hard_reset":
        entry_id = await _zwave_entry_id(client)
        if not entry_id:
            integration_required("zwave", "zwave_js")
        result = await ws_call(
            client,
            "zwave_js/hard_reset_controller",
            entry_id=entry_id,
            context={"entry_id": entry_id},
        )
        return ok("zwave", "hard_reset", result=result, long_running=True)

    # Unreachable: dispatcher validates action against SUPPORTED first.
    raise AssertionError(f"unhandled zwave action: {action}")


validate_inclusion = legacy_errors_sync(_validate_inclusion)
