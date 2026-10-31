"""OTBR operations through the installed OTBRData owner and Core helpers."""

from functools import partial
from typing import Any

from ha_mcp.runtime.radio_values import ok
from ha_mcp.tool_api import ToolFailure

from ..optional_integrations import loaded_integration_module
from .radio_access import RadioSession, unavailable


async def _owners(session: RadioSession) -> list[tuple[Any, Any, str]]:
    entries = session.hass.config_entries.async_loaded_entries("otbr")
    if not entries:
        raise unavailable("otbr")
    result = []
    for item in entries:
        entry = session.entry("otbr", item.entry_id)
        owner = session.retain(
            partial(getattr, entry, "runtime_data"), entry.runtime_data
        )
        address = await session.read(owner.get_extended_address)
        result.append((entry, owner, address.hex()))
    return result


async def handle_otbr(
    session: RadioSession, action: str, args: dict[str, Any]
) -> dict[str, Any]:
    owners = await _owners(session)
    if action == "network_status":
        routers = {}
        session.validate("otbr/info", {})
        for _entry, owner, address in owners:
            agent = await session.read(owner.get_border_agent_id)
            dataset = await session.read(owner.get_active_dataset)
            tlvs = await session.read(owner.get_active_dataset_tlvs)
            routers[address] = {
                "active_dataset_tlvs": tlvs.hex() if tlvs else None,
                "border_agent_id": agent.hex() if agent else None,
                "channel": dataset.channel if dataset else None,
                "extended_address": address,
                "extended_pan_id": dataset.extended_pan_id.lower()
                if dataset and dataset.extended_pan_id
                else None,
                "url": owner.url,
            }
        return ok(
            "thread",
            action,
            config_entry_id=owners[0][0].entry_id,
            border_routers=routers,
        )
    address = args.get("extended_address") or owners[0][2]
    owner = next((owner for _, owner, item in owners if item == address), None)
    if owner is None:
        raise ToolFailure(
            "RESOURCE_NOT_FOUND", "Thread border router not found", status="not_started"
        )
    fields = {"extended_address": address}
    if action == "set_network":
        fields["dataset_id"] = args["dataset_id"]
    elif action == "set_channel":
        fields["channel"] = args["channel"]
    payload = session.validate("otbr/" + action, fields)
    if action == "set_channel":
        is_multiprotocol_url = loaded_integration_module(
            session.hass, "homeassistant_hardware.silabs_multiprotocol_addon"
        ).is_multiprotocol_url
        PENDING_DATASET_DELAY_TIMER = loaded_integration_module(
            session.hass, "otbr.websocket_api"
        ).PENDING_DATASET_DELAY_TIMER

        if is_multiprotocol_url(owner.url):
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                "Channel change is unavailable in multiprotocol mode",
                status="not_started",
            )
        await session.effect(lambda: owner.set_channel(payload["channel"]))
        result: Any = {"delay": PENDING_DATASET_DELAY_TIMER / 1000}
    elif action == "set_network":
        await _set_network(session, owner, payload["dataset_id"])
        result = None
    else:
        await _create_network(session, owner)
        result = None
    return ok("thread", action, result=result, extended_address=address)


async def _set_network(session: RadioSession, owner: Any, identifier: str) -> None:
    otbr_util = loaded_integration_module(session.hass, "otbr.util")
    get_allowed_channel = otbr_util.get_allowed_channel
    update_issues = otbr_util.update_issues
    otbr_websocket_api = loaded_integration_module(session.hass, "otbr.websocket_api")
    MeshcopTLVType = otbr_websocket_api.MeshcopTLVType
    tlv_parser = otbr_websocket_api.tlv_parser

    from .radio_thread import dataset_owner

    store = await dataset_owner(session)
    row = store.async_get(identifier)
    if row is None:
        raise ToolFailure(
            "RESOURCE_NOT_FOUND", "Thread dataset not found", status="not_started"
        )
    session.retain(lambda: store.async_get(identifier), row, target=True)
    session.freeze(row.to_json)
    dataset = tlv_parser.parse_tlv(row.tlv)
    channel = dataset.get(MeshcopTLVType.CHANNEL)
    allowed = await session.read(lambda: get_allowed_channel(session.hass, owner.url))
    if allowed and (channel is None or channel.channel != allowed):
        raise ToolFailure(
            "VALIDATION_INVALID_PARAMETER",
            "Thread dataset channel conflicts with the shared ZHA radio",
            status="not_started",
        )
    encoded = bytes.fromhex(row.tlv)
    await session.effect(lambda: owner.set_enabled(False))
    await session.effect(lambda: owner.set_active_dataset_tlvs(encoded))
    await session.effect(lambda: owner.set_enabled(True))
    await session.effect(lambda: update_issues(session.hass, owner, encoded))


async def _create_network(session: RadioSession, owner: Any) -> None:
    DEFAULT_CHANNEL = loaded_integration_module(
        session.hass, "otbr.const"
    ).DEFAULT_CHANNEL
    otbr_util = loaded_integration_module(session.hass, "otbr.util")
    compose_default_network_name = otbr_util.compose_default_network_name
    generate_random_pan_id = otbr_util.generate_random_pan_id
    get_allowed_channel = otbr_util.get_allowed_channel
    update_issues = otbr_util.update_issues
    python_otbr_api = loaded_integration_module(
        session.hass, "otbr.websocket_api"
    ).python_otbr_api

    from .radio_thread import dataset_owner

    store = await dataset_owner(session)
    channel = (
        await session.read(lambda: get_allowed_channel(session.hass, owner.url))
        or DEFAULT_CHANNEL
    )
    pan_id = generate_random_pan_id()
    planned = python_otbr_api.ActiveDataSet(
        channel=channel,
        network_name=compose_default_network_name(pan_id),
        pan_id=pan_id,
    )
    await session.effect(lambda: owner.set_enabled(False))
    await session.effect(lambda: owner.factory_reset(session.hass))
    await session.effect(lambda: owner.create_active_dataset(planned))
    await session.effect(lambda: owner.set_enabled(True))
    tlvs = await session.read(owner.get_active_dataset_tlvs)
    if not tlvs:
        raise RuntimeError(
            "The border router returned no active dataset after creation"
        )
    await session.sync_effect(lambda: store.async_add("otbr", tlvs.hex(), None, None))
    await session.effect(lambda: update_issues(session.hass, owner, tlvs))
