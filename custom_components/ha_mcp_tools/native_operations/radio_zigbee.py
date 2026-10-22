"""ZHA management via its gateway, device/group owners and registered services."""

from typing import Any

from ha_mcp.runtime.radio_values import ok
from ha_mcp.tool_api import ToolFailure

from .radio_access import RadioSession, unavailable
from .registry_access import registry_json


def _gateway(session: RadioSession) -> tuple[Any, Any, Any]:
    entry = session.entry("zha")
    from homeassistant.components.zha.const import DATA_ZHA

    data = session.hass.data.get(DATA_ZHA)
    if data is None or data.gateway_proxy is None:
        raise unavailable("zha gateway")
    session.retain(lambda: session.hass.data.get(DATA_ZHA), data)
    proxy = session.retain(lambda: data.gateway_proxy, data.gateway_proxy)
    gateway = session.retain(lambda: proxy.gateway, proxy.gateway)
    session.retain(
        lambda: gateway.application_controller, gateway.application_controller
    )
    return entry, proxy, gateway


def _ieee(session: RadioSession, identifier: str) -> str:
    row = session.device(identifier)
    for domain, value in row.identifiers:
        if domain == "zha":
            return str(value)
    for kind, value in row.connections:
        if kind == "ieee":
            return str(value)
    raise ToolFailure(
        "VALIDATION_INVALID_PARAMETER",
        "Device has no ZHA IEEE address",
        status="not_started",
    )


def _device(session: RadioSession, gateway: Any, ieee: Any) -> Any:
    value = gateway.get_device(ieee)
    if value is None:
        raise ToolFailure(
            "RESOURCE_NOT_FOUND", "ZHA device is unavailable", status="not_started"
        )
    return session.retain(lambda: gateway.get_device(ieee), value, target=True)


async def handle_zigbee(
    session: RadioSession, action: str, args: dict[str, Any]
) -> dict[str, Any]:
    entry, proxy, gateway = _gateway(session)
    if action == "network_status":
        from homeassistant.components.zha.api import (
            CONF_DEVICE,
            async_get_active_network_settings,
            async_get_radio_type,
        )

        session.validate("zha/network/settings", {})
        network = {
            "radio_type": async_get_radio_type(session.hass, entry).name,
            "device": gateway.application_controller.config[CONF_DEVICE],
            "settings": async_get_active_network_settings(session.hass).as_dict(),
        }
        return ok(
            "zigbee",
            action,
            config_entry_id=entry.entry_id,
            network=registry_json(network),
            note="Current Core settings; this read does not start a topology scan.",
        )
    if action == "firmware_update":
        entity_id = await session.firmware(args["device_id"], "zha")
        return ok("zigbee", action, entity_id=entity_id, long_running=True)
    if action in {
        "diagnostics",
        "permit_join",
        "reconfigure",
        "remove_device",
        "cluster_read",
        "cluster_write",
        "cluster_command",
    }:
        return await _device_action(session, proxy, gateway, action, args)
    if action.startswith("group_"):
        from .radio_zigbee_groups import groups

        return await groups(session, proxy, gateway, action, args)
    if action in {"bind", "unbind"}:
        from .radio_zigbee_groups import bind

        return await bind(session, gateway, action, args)
    return await _network_action(session, gateway, action, args)


async def _device_action(
    session: RadioSession, proxy: Any, gateway: Any, action: str, args: dict[str, Any]
) -> dict[str, Any]:
    ieee = _ieee(session, args["device_id"]) if args.get("device_id") else None
    if action == "permit_join":
        payload = session.validate(
            "zha/devices/permit", {"ieee": ieee, "duration": args.get("duration", 60)}
        )
        if payload.get("ieee") is not None:
            _device(session, gateway, payload["ieee"])
        await session.effect(
            lambda: gateway.application_controller.permit(
                time_s=payload["duration"], node=payload.get("ieee")
            )
        )
        return ok(
            "zigbee", action, duration=payload["duration"], ieee=ieee, result=None
        )
    payload = session.validate("zha/device", {"ieee": ieee})
    device = _device(session, gateway, payload["ieee"])
    if action == "diagnostics":
        device_proxy = proxy.device_proxies.get(payload["ieee"])
        if device_proxy is None:
            raise unavailable("zha device proxy")
        return ok(
            "zigbee",
            action,
            ieee=ieee,
            diagnostics=registry_json(device_proxy.zha_device_info),
        )
    if action == "reconfigure":
        session.validate("zha/devices/reconfigure", {"ieee": ieee})
        await session.effect(lambda: gateway.async_reinterview_device(payload["ieee"]))
        return ok(
            "zigbee",
            action,
            ieee=ieee,
            result=None,
            note="Core re-interview completed before this response.",
        )
    if action == "remove_device":
        result = await session.service("zha", "remove", {"ieee": ieee})
        return ok("zigbee", action, ieee=ieee, result=result)
    return await _cluster(session, device, action, ieee, args)


async def _cluster(
    session: RadioSession,
    device: Any,
    action: str,
    ieee: str | None,
    args: dict[str, Any],
) -> dict[str, Any]:
    values = {
        key: args.get(key)
        for key in ("endpoint_id", "cluster_id", "attribute", "manufacturer")
    }
    values.update(ieee=ieee, cluster_type=args.get("cluster_type", "in"))
    if action == "cluster_read":
        from homeassistant.components.zha.websocket_api import (
            ZIGPY_UNDEFINED as UNDEFINED,
        )

        payload = session.validate("zha/devices/clusters/attributes/value", values)
        cluster = device.async_get_cluster(
            payload["endpoint_id"],
            payload["cluster_id"],
            cluster_type=payload["cluster_type"],
        )
        session.retain(
            lambda: device.async_get_cluster(
                payload["endpoint_id"],
                payload["cluster_id"],
                cluster_type=payload["cluster_type"],
            ),
            cluster,
            target=True,
        )
        success, _failure = await session.read(
            lambda: cluster.read_attributes(
                [payload["attribute"]],
                allow_cache=False,
                only_cache=False,
                manufacturer=payload.get("manufacturer", UNDEFINED),
            )
        )
        return ok(
            "zigbee", action, ieee=ieee, value=str(success.get(payload["attribute"]))
        )
    if action == "cluster_write":
        values["value"] = args.get("value")
        service = "set_zigbee_cluster_attribute"
    else:
        values.pop("attribute")
        values.update(
            {
                key: args.get(key)
                for key in ("command", "command_type", "params", "args")
            }
        )
        service = "issue_zigbee_cluster_command"
    result = await session.service(
        "zha",
        service,
        {key: value for key, value in values.items() if value is not None},
    )
    return ok("zigbee", action, ieee=ieee, result=result)


async def _network_action(
    session: RadioSession, gateway: Any, action: str, args: dict[str, Any]
) -> dict[str, Any]:
    app = gateway.application_controller
    backups = session.retain(lambda: app.backups, app.backups)
    if action == "network_backup":
        session.validate("zha/network/backups/create", {})
        backup = await session.effect(lambda: backups.create_backup(load_devices=True))
        return ok(
            "zigbee",
            action,
            backup={"backup": backup.as_dict(), "is_complete": backup.is_complete()},
        )
    if action == "network_restore":
        payload = session.validate(
            "zha/network/backups/restore", {"backup": args["backup"]}
        )
        await session.effect(lambda: backups.restore_backup(payload["backup"]))
        return ok("zigbee", action, result=None)
    payload = session.validate(
        "zha/network/change_channel", {"new_channel": args["new_channel"]}
    )
    channel = payload["new_channel"]
    if channel == "auto":
        from homeassistant.components.zha.api import Channels, pick_optimal_channel

        energy = await session.read(
            lambda: app.energy_scan(
                channels=Channels.ALL_CHANNELS, duration_exp=4, count=1
            )
        )
        channel = pick_optimal_channel(energy)
    await session.effect(lambda: app.move_network_to_channel(channel))
    return ok("zigbee", action, new_channel=payload["new_channel"], result=None)
