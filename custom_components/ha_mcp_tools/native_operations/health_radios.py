"""Read the loaded radio owners; never invoke their WebSocket handlers."""

from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.owned_executor import await_owned_io

from ..optional_integrations import loaded_integration_module
from .registry_access import Authorize, authorize_actor

# The network-status presentation in Core 2026.10.2 has no standalone producer.
# This bounded property projection retains its public controller fields; Node
# rows themselves use Core's node_status serializer below.
_CONTROLLER_FIELDS = (
    "home_id",
    "sdk_version",
    "own_node_id",
    "is_primary",
    "is_using_home_id_from_other_network",
    "was_real_primary",
    "is_suc",
    "node_type",
    "firmware_version",
    "manufacturer_id",
    "product_id",
    "product_type",
    "supported_function_types",
    "suc_node_id",
    "supports_timers",
    "supports_long_range",
    "is_rebuilding_routes",
    "inclusion_state",
    "rf_region",
    "status",
)


async def zwave_status(
    hass: HomeAssistant, entry_id: str, actor: CallContext, authorize: Authorize
) -> dict[str, Any]:
    node_status = loaded_integration_module(hass, "zwave_js.api").node_status

    await authorize_actor(actor, authorize)
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        raise ToolFailure(
            "RADIO_UNAVAILABLE", "Z-Wave JS entry is not loaded", status="not_started"
        )
    client = entry.runtime_data.client
    driver = client.driver
    if driver is None:
        raise ToolFailure(
            "RADIO_UNAVAILABLE", "Z-Wave JS driver is not ready", status="not_started"
        )
    controller = driver.controller
    state = await await_owned_io(controller.async_get_state())
    await authorize_actor(actor, authorize)
    if (
        hass.config_entries.async_get_entry(entry_id) is not entry
        or entry.state is not ConfigEntryState.LOADED
        or entry.runtime_data.client is not client
        or client.driver is not driver
        or driver.controller is not controller
    ):
        raise ToolFailure(
            "CONFIG_CONFLICT",
            "Z-Wave JS owner changed during the status read",
            status="not_started",
        )
    controller.update(state)
    return {
        "controller": {
            **{name: getattr(controller, name) for name in _CONTROLLER_FIELDS},
            "type": controller.controller_type,
            "is_sis_present": controller.is_SIS_present,
            "nodes": [node_status(node) for node in controller.nodes.values()],
        }
    }


async def thread_status(
    hass: HomeAssistant, actor: CallContext, authorize: Authorize
) -> dict[str, Any]:
    DOMAIN = loaded_integration_module(hass, "otbr.const").DOMAIN

    await authorize_actor(actor, authorize)
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ToolFailure(
            "RADIO_UNAVAILABLE", "No OTBR API loaded", status="not_started"
        )
    result = {}
    for entry in entries:
        owner = entry.runtime_data
        values = []
        # The public health summary needs no active-dataset TLVs or network key.
        for read in (
            owner.get_border_agent_id,
            owner.get_active_dataset,
            owner.get_extended_address,
        ):
            await authorize_actor(actor, authorize)
            values.append(await await_owned_io(read()))
            await authorize_actor(actor, authorize)
            if (
                hass.config_entries.async_get_entry(entry.entry_id) is not entry
                or entry.state is not ConfigEntryState.LOADED
                or entry.runtime_data is not owner
            ):
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "Thread border-router owner changed during the status read",
                    status="not_started",
                )
        border_agent, dataset, address = values
        if border_agent is None:
            raise ToolFailure(
                "RADIO_UNAVAILABLE",
                "The loaded border router has no border agent id",
                status="not_started",
            )
        result[address.hex()] = {
            "border_agent_id": border_agent.hex(),
            "channel": dataset.channel if dataset else None,
            "extended_pan_id": dataset.extended_pan_id.lower()
            if dataset and dataset.extended_pan_id
            else None,
        }
    return result
