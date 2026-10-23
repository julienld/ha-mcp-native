"""Thread datasets use Core's lazy singleton; discovery remains bounded."""

import asyncio
from dataclasses import asdict
from typing import Any

from ha_mcp.runtime.radio_values import ok
from ha_mcp.tool_api import ToolFailure
from ha_mcp.utils.owned_executor import await_owned_io

from ..optional_integrations import loaded_integration_module
from .radio_access import RadioSession
from .registry_access import registry_json


async def dataset_owner(session: RadioSession) -> Any:
    module = loaded_integration_module(session.hass, "thread.dataset_store")
    session.pins.append(lambda: "thread" in session.hass.config.components)
    owner = await session.read(
        lambda: await_owned_io(module.async_get_store(session.hass))
    )
    if session.hass.data.get(module.DATA_STORE) is not owner:
        raise ToolFailure(
            "CONFIG_CONFLICT",
            "Core Thread dataset owner changed while loading",
            status="not_started",
        )
    session.retain(lambda: session.hass.data.get(module.DATA_STORE), owner)
    return owner


def _dataset_image(owner: Any) -> dict[str, Any]:
    return {
        "datasets": {key: row.to_json() for key, row in owner.datasets.items()},
        "preferred": owner.preferred_dataset,
    }


async def handle_thread(
    session: RadioSession, action: str, args: dict[str, Any]
) -> dict[str, Any]:
    if action in {"network_status", "create_network", "set_network", "set_channel"}:
        from .radio_otbr import handle_otbr

        return await handle_otbr(session, action, args)
    if action == "discover_routers":
        return await _discover(session)
    owner = await dataset_owner(session)
    command = "add_dataset_tlv" if action == "add_dataset" else action
    fields = ("source", "tlv") if action == "add_dataset" else ("dataset_id",)
    payload = session.validate(
        "thread/" + command,
        {key: args.get(key) for key in fields} if action != "list_datasets" else {},
    )
    if action == "list_datasets":
        datasets = [
            {
                "channel": row.channel,
                "created": row.created,
                "dataset_id": row.id,
                "extended_pan_id": row.extended_pan_id,
                "network_name": row.network_name,
                "pan_id": row.pan_id,
                "preferred": row.id == owner.preferred_dataset,
                "preferred_border_agent_id": row.preferred_border_agent_id,
                "preferred_extended_address": row.preferred_extended_address,
                "source": row.source,
            }
            for row in owner.datasets.values()
        ]
        return ok("thread", action, datasets=registry_json({"datasets": datasets}))
    session.freeze(lambda: _dataset_image(owner))
    if action != "add_dataset" and payload["dataset_id"] not in owner.datasets:
        raise ToolFailure(
            "RESOURCE_NOT_FOUND", "Thread dataset not found", status="not_started"
        )
    if action == "delete_dataset" and owner.preferred_dataset == payload["dataset_id"]:
        raise ToolFailure(
            "VALIDATION_INVALID_PARAMETER",
            "Invalid attempt to remove preferred dataset; select another preferred Thread dataset before deleting this one",
            status="not_started",
        )

    async def mutate() -> Any:
        if action == "add_dataset":
            result = owner.async_add(payload["source"], payload["tlv"], None, None)
            return {"result": str(result)}
        if payload["dataset_id"] not in owner.datasets:
            raise ToolFailure(
                "RESOURCE_NOT_FOUND", "Thread dataset not found", status="not_started"
            )
        if action == "delete_dataset":
            owner.async_delete(payload["dataset_id"])
        else:
            owner.preferred_dataset = payload["dataset_id"]
        return None

    result = await session.effect(mutate)
    return ok("thread", action, result=result)


async def _discover(session: RadioSession) -> dict[str, Any]:
    ThreadRouterDiscovery = loaded_integration_module(
        session.hass, "thread.discovery"
    ).ThreadRouterDiscovery

    session.validate("thread/discover_routers", {})
    routers: dict[str, Any] = {}
    active = True

    def found(key: str, value: Any) -> None:
        if active:
            routers[key] = asdict(value)

    def removed(key: str) -> None:
        if active:
            routers.pop(key, None)

    discovery = ThreadRouterDiscovery(session.hass, found, removed)

    async def scan() -> dict[str, Any]:
        nonlocal active
        try:
            await discovery.async_start()
            await asyncio.sleep(3)
        finally:
            active = False
            await discovery.async_stop()
        return registry_json(routers)

    result = await session.effect(scan)
    return ok(
        "thread",
        "discover_routers",
        routers=result,
        long_running=False,
        discovery_seconds=3,
        complete=False,
        note="Results observed during a three-second discovery window. This call's listener has stopped; later advertisements are not included.",
    )
