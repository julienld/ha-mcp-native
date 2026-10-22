"""Stage real persisted configuration and observe real config-entry reloads."""

from typing import Any

from homeassistant.components.config.view import _write
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.ha_mcp_tools.native_operations.core_views import config_view

SCRIPT_ID = "native_lifecycle"


async def stage_system_script(hass: HomeAssistant, name: str) -> dict[str, str]:
    """Change scripts.yaml without calling its reload hook."""
    owner = config_view(hass, "script")
    async with owner.mutation_lock:
        current = await owner.read_config(hass)
        owner._write_value(
            hass,
            current,
            SCRIPT_ID,
            {
                "alias": name,
                "sequence": [
                    {
                        "action": "input_boolean.turn_on",
                        "target": {"entity_id": "input_boolean.native_light"},
                    }
                ],
            },
        )
        await hass.async_add_executor_job(_write, hass.config.path(owner.path), current)
    return {"entity_id": f"script.{SCRIPT_ID}", "name": name}


async def seed_system_actions(hass: HomeAssistant) -> dict[str, str]:
    initial = await stage_system_script(hass, "Native lifecycle before reload")
    await hass.services.async_call("script", "reload", {}, blocking=True)
    flow = await hass.config_entries.flow.async_init(
        "native_fixture",
        context={"source": "user"},
        data={"name": "Native lifecycle entry"},
    )
    entry = flow["result"]
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", "native_fixture", f"{entry.entry_id}:sensor"
    )
    assert entity_id is not None and hass.states.get(entity_id) is not None
    records: list[dict[str, bool]] = []

    def observe(event: Any) -> None:
        if event.data.get("entity_id") == entity_id:
            records.append(
                {
                    "removed": event.data.get("new_state") is None,
                    "added": event.data.get("old_state") is None,
                }
            )

    remove = hass.bus.async_listen("state_changed", observe)
    hass.data["native_fixture"]["system_actions"] = {
        "entry_id": entry.entry_id,
        "entity_id": entity_id,
        "records": records,
        "remove": remove,
        "initial_manager": hass.data["ha_mcp_tools"]["manager"],
    }
    return {**initial, "entry_id": entry.entry_id, "sensor_id": entity_id}


def system_actions_probe(hass: HomeAssistant) -> dict[str, Any]:
    data = hass.data["native_fixture"].get("system_actions", {})
    manager = hass.data.get("ha_mcp_tools", {}).get("manager")
    entry = hass.config_entries.async_get_entry(data.get("entry_id", ""))
    return {
        "manager_identity": id(manager) if manager is not None else None,
        "manager_entry_id": manager.entry.entry_id if manager is not None else None,
        "entry_state": entry.state.value if entry is not None else None,
        "records": list(data.get("records", [])),
        "pending": len(manager.system_lifecycle.pending)
        if manager is not None and manager.system_lifecycle is not None
        else 0,
    }
