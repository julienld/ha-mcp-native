"""Z-Wave JS calls through loaded entry/client/driver/node owners."""

from typing import Any

from ha_mcp.runtime.radio_values import ok
from ha_mcp.runtime.radio_zwave_values import (
    diagnostics,
    network_status,
)

from ..optional_integrations import loaded_integration_module
from .radio_access import RadioSession, unavailable


def _controller(session: RadioSession, args: dict[str, Any]) -> tuple[Any, Any, Any]:
    if not session.hass.config_entries.async_loaded_entries("zwave_js"):
        raise unavailable("zwave_js")
    entry_id = None
    if args.get("device_id"):
        row = session.device(args["device_id"])
        owners = session.device_config_entries(args["device_id"], row)
        entries = session.hass.config_entries.async_loaded_entries("zwave_js")
        entry_id = next(
            (entry.entry_id for entry in entries if entry.entry_id in owners),
            None,
        )
        if entry_id is None:
            raise unavailable("zwave_js device entry")
    entry = session.entry("zwave_js", entry_id)
    client = session.runtime_owner(entry, "client")
    if client.driver is None:
        raise unavailable("zwave_js driver")
    driver = session.retain(lambda: client.driver, client.driver)
    controller = session.retain(lambda: driver.controller, driver.controller)
    return entry, driver, controller


def _node(session: RadioSession, device_id: str) -> Any:
    async_get_node_from_device_id = loaded_integration_module(
        session.hass, "zwave_js.helpers"
    ).async_get_node_from_device_id

    node = async_get_node_from_device_id(session.hass, device_id)
    return session.retain(
        lambda: async_get_node_from_device_id(session.hass, device_id),
        node,
        target=True,
    )


async def handle_zwave(
    session: RadioSession, action: str, args: dict[str, Any]
) -> dict[str, Any]:
    entry, driver, controller = _controller(session, args)
    if action == "network_status":
        from .health_radios import zwave_status

        net = await zwave_status(
            session.hass, entry.entry_id, session.actor, session.authorize
        )
        session.verify()
        return network_status(entry.entry_id, net)
    if action == "firmware_update":
        entity_id = await session.firmware(args["device_id"], "zwave_js")
        return ok("zwave", action, entity_id=entity_id, long_running=True)
    if (
        action in {"add", "hard_reset"}
        or (action == "remove_device" and not args.get("failed"))
        or (action == "rebuild_routes" and args.get("scope") == "network")
    ):
        from .radio_zwave_network import network_action

        return await network_action(session, entry, driver, controller, action, args)
    node = _node(session, args["device_id"])
    if action in {"get_config_params", "get_config_param"}:
        from .radio_zwave_parameters import parameters

        return await parameters(session, node, action, args)
    if action == "diagnostics":
        node_status = loaded_integration_module(
            session.hass, "zwave_js.api"
        ).node_status

        session.validate("zwave_js/node_status", {"device_id": args["device_id"]})
        return diagnostics(node_status(node))
    if action == "ping":
        result = await session.service(
            "zwave_js", "ping", {"device_id": args["device_id"]}, writes=False
        )
        return ok("zwave", action, result=result)
    return await _node_action(session, controller, node, action, args)


async def _node_action(
    session: RadioSession, controller: Any, node: Any, action: str, args: dict[str, Any]
) -> dict[str, Any]:
    commands = {
        "remove_device": "remove_failed_node",
        "reinterview": "refresh_node_info",
        "rebuild_routes": "rebuild_node_routes",
        "set_config_param": "set_config_parameter",
    }
    values = {"device_id": args["device_id"]}
    if action == "set_config_param":
        values.update(
            {key: args.get(key) for key in ("property", "property_key", "value")}
        )
        values["endpoint"] = args.get("endpoint", 0)
    payload = session.validate("zwave_js/" + commands[action], values)
    if action == "remove_device":
        await session.effect(lambda: controller.async_remove_failed_node(node))
        return ok("zwave", action, mode="failed", result=None)
    if action == "reinterview":
        await session.effect(node.async_refresh_info)
        return ok("zwave", action, result=None, long_running=True)
    if action == "rebuild_routes":
        result = await session.effect(
            lambda: controller.async_rebuild_node_routes(node)
        )
        return ok("zwave", action, scope="node", result=result, long_running=True)
    async_set_config_parameter = loaded_integration_module(
        session.hass, "zwave_js.api"
    ).async_set_config_parameter

    value, status = await session.effect(
        lambda: async_set_config_parameter(
            node,
            payload["value"],
            payload["property"],
            property_key=payload.get("property_key"),
            endpoint=payload["endpoint"],
        )
    )
    return ok(
        "zwave", action, result={"value_id": value.value_id, "status": status.status}
    )
