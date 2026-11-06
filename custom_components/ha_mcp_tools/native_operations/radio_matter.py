"""Matter operations through the loaded adapter and its real protocol client."""

from typing import Any

from ha_mcp.runtime.radio_values import ok
from ha_mcp.tool_api import ToolFailure

from ..optional_integrations import loaded_integration_module
from .radio_access import RadioSession
from .registry_access import registry_json

_COMMANDS = {
    "diagnostics": "node_diagnostics",
    "ping": "ping_node",
    "commission": "commission",
    "commission_on_network": "commission_on_network",
    "share_out": "open_commissioning_window",
    "interview": "interview_node",
    "remove_fabric": "remove_matter_fabric",
    "set_thread": "set_thread",
    "set_wifi_credentials": "set_wifi_credentials",
}
_FIELDS = {
    "commission": ("code", "network_only"),
    "commission_on_network": ("pin", "ip_addr"),
    "set_thread": ("thread_operation_dataset",),
    "set_wifi_credentials": ("network_name", "password"),
    "remove_fabric": ("device_id", "fabric_index"),
}


async def handle_matter(
    session: RadioSession, action: str, args: dict[str, Any]
) -> dict[str, Any]:
    entry = session.entry("matter")
    adapter = session.runtime_owner(entry, "adapter")
    client = session.retain(lambda: adapter.matter_client, adapter.matter_client)
    if action == "network_status":
        return ok(
            "matter",
            action,
            config_entry_id=entry.entry_id,
            note="Matter exposes health per node; call action='diagnostics' with a device_id for node-level network type, availability and fabrics.",
        )
    if action == "firmware_update":
        entity_id = await session.firmware(args["device_id"], "matter")
        return ok("matter", action, entity_id=entity_id, long_running=True)
    fields = _FIELDS.get(action, ("device_id",))
    proposal = {key: args.get(key) for key in fields}
    if action == "commission":
        proposal["network_only"] = bool(args.get("network_only", False))
    payload = session.validate("matter/" + _COMMANDS[action], proposal)
    if "device_id" in payload:
        node_from_ha_device_id = loaded_integration_module(
            session.hass, "matter.helpers"
        ).node_from_ha_device_id

        session.device(payload["device_id"])
        node = node_from_ha_device_id(session.hass, payload["device_id"])
        if node is None:
            raise ToolFailure(
                "RESOURCE_NOT_FOUND", "Matter node is unavailable", status="not_started"
            )
        session.retain(
            lambda: node_from_ha_device_id(session.hass, payload["device_id"]),
            node,
            target=True,
        )
        return await _node_action(session, client, node, action, payload)
    return await _network_action(session, client, action, payload)


def _model_result(session: RadioSession, value: Any) -> dict[str, Any]:
    dataclass_to_dict = loaded_integration_module(
        session.hass, "matter.api"
    ).dataclass_to_dict

    return registry_json(dataclass_to_dict(value))


async def _node_action(
    session: RadioSession, client: Any, node: Any, action: str, payload: dict[str, Any]
) -> dict[str, Any]:
    if action == "diagnostics":
        result = _model_result(
            session,
            await session.read(lambda: client.node_diagnostics(node_id=node.node_id)),
        )
        if "ip_adresses" in result:
            result["ip_addresses"] = result.pop("ip_adresses")
        return ok("matter", action, diagnostics=result)
    if action == "ping":
        result = await session.read(lambda: client.ping_node(node_id=node.node_id))
        return ok("matter", action, reachability=result)
    if action == "share_out":
        result = _model_result(
            session,
            await session.effect(
                lambda: client.open_commissioning_window(node_id=node.node_id)
            ),
        )
        return ok("matter", action, commissioning=result)
    if action == "interview":
        await session.effect(lambda: client.interview_node(node_id=node.node_id))
        return ok("matter", action, result=None, long_running=True)
    diagnostics = _model_result(
        session,
        await session.read(lambda: client.node_diagnostics(node_id=node.node_id)),
    )
    if payload["fabric_index"] == diagnostics.get("active_fabric_index"):
        raise ToolFailure(
            "VALIDATION_INVALID_PARAMETER",
            "remove_fabric only removes another controller's fabric; use ha_remove_device to detach Home Assistant",
            status="not_started",
        )
    # Fabric membership is a live device read; Core offers no atomic conditional
    # removal. The same client and node remain pinned through the read and write.
    await session.effect(
        lambda: client.remove_matter_fabric(
            node_id=node.node_id, fabric_index=payload["fabric_index"]
        )
    )
    return ok("matter", action, result=None, fabric_index=payload["fabric_index"])


async def _network_action(
    session: RadioSession, client: Any, action: str, payload: dict[str, Any]
) -> dict[str, Any]:
    if action == "commission":
        await session.effect(
            lambda: client.commission_with_code(
                payload["code"], network_only=payload["network_only"]
            )
        )
    elif action == "commission_on_network":
        await session.effect(
            lambda: client.commission_on_network(
                payload["pin"], ip_addr=payload.get("ip_addr")
            )
        )
    elif action == "set_thread":
        await session.effect(
            lambda: client.set_thread_operational_dataset(
                payload["thread_operation_dataset"]
            )
        )
    else:
        await session.effect(
            lambda: client.set_wifi_credentials(
                ssid=payload["network_name"], credentials=payload["password"]
            )
        )
    return ok("matter", action, result=None)
