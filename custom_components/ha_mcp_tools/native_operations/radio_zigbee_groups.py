"""ZHA group and binding owners with guards before each physical operation."""

from functools import partial
from typing import Any

from ha_mcp.runtime.radio_values import ok
from ha_mcp.tool_api import ToolFailure

from .radio_access import RadioSession
from .registry_access import registry_json


async def groups(
    session: RadioSession, proxy: Any, gateway: Any, action: str, args: dict[str, Any]
) -> dict[str, Any]:
    suffix = {
        "group_add": "add",
        "group_remove": "remove",
        "group_members_add": "members/add",
        "group_members_remove": "members/remove",
    }[action]
    fields = {
        "group_add": ("group_name", "group_id", "members"),
        "group_remove": ("group_ids",),
    }.get(action, ("group_id", "members"))
    payload = session.validate(
        "zha/group/" + suffix, {key: args.get(key) for key in fields}
    )
    if action == "group_add":
        group = await session.effect(
            lambda: gateway.async_create_zigpy_group(
                payload["group_name"], payload.get("members"), payload.get("group_id")
            )
        )
        result = proxy.group_proxies[group.group_id].group_info
    elif action == "group_remove":
        for identifier in payload["group_ids"]:
            await session.effect(partial(gateway.async_remove_zigpy_group, identifier))
        return ok(
            "zigbee",
            action,
            result=registry_json(
                [group.group_info for group in proxy.group_proxies.values()]
            ),
        )
    else:
        identifier = payload["group_id"]
        group = gateway.groups.get(identifier)
        if group is None:
            raise ToolFailure(
                "RESOURCE_NOT_FOUND", "ZHA group not found", status="not_started"
            )
        session.retain(lambda: gateway.groups.get(identifier), group, target=True)
        if action == "group_members_add":
            await session.effect(lambda: group.async_add_members(payload["members"]))
        else:
            await session.effect(lambda: group.async_remove_members(payload["members"]))
        result = proxy.get_group_proxy(identifier).group_info
    return ok("zigbee", action, group=registry_json(result))


async def bind(
    session: RadioSession, gateway: Any, action: str, args: dict[str, Any]
) -> dict[str, Any]:
    from homeassistant.components.zha.websocket_api import (
        get_matched_clusters,
        zdo_types,
    )

    Status, ZDOCmd = zdo_types.Status, zdo_types.ZDOCmd

    from .radio_zigbee import _device

    payload = session.validate(
        "zha/devices/" + action,
        {key: args.get(key) for key in ("source_ieee", "target_ieee")},
    )
    source = _device(session, gateway, payload["source_ieee"])
    target = _device(session, gateway, payload["target_ieee"])
    pairs = await session.read(lambda: get_matched_clusters(source, target))
    operation = ZDOCmd.Bind_req if action == "bind" else ZDOCmd.Unbind_req
    zdo = session.retain(lambda: source.device.zdo, source.device.zdo, target=True)
    for pair in pairs:
        result = await session.effect(
            partial(
                zdo.request,
                operation,
                source.ieee,
                pair.source_cluster.endpoint.endpoint_id,
                pair.source_cluster.cluster_id,
                pair.destination_address,
            )
        )
        if result and result[0] != Status.SUCCESS:
            raise RuntimeError(
                "A Zigbee binding request was not acknowledged as successful"
            )
    return ok("zigbee", action, result=None)
