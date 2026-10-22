"""Real Core service and registry owners for bulk confirmation/uncertainty checks."""

from typing import Any

import probatio
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er


async def seed_bulk(hass: HomeAssistant) -> dict[str, Any]:
    domain = "native_fixture"
    entry_id = hass.data[domain]["sensor_entry_id"]
    areas = ar.async_get(hass)
    area = areas.async_get_area_by_name("Native bulk room") or areas.async_create(
        "Native bulk room"
    )
    registry = er.async_get(hass)
    ids = {}
    for key in ("a", "b", "uncertain"):
        entry = registry.async_get_or_create(
            domain,
            domain,
            f"bulk_{key}",
            config_entry_id=entry_id,
            suggested_object_id=f"native_bulk_{key}",
        )
        registry.async_update_entity(
            entry.entity_id, area_id=area.id if key != "uncertain" else None
        )
        hass.states.async_set(entry.entity_id, "off", {"native_dispatches": 0})
        ids[key] = entry.entity_id

    async def change(call: ServiceCall) -> None:
        for entity_id in call.data["entity_id"]:
            old = hass.states.get(entity_id)
            assert old is not None
            state = (
                "off"
                if call.service == "turn_off"
                or (call.service == "toggle" and old.state == "on")
                else "on"
            )
            hass.states.async_set(
                entity_id,
                state,
                {"native_dispatches": old.attributes["native_dispatches"] + 1},
                context=call.context,
            )
            if entity_id == ids["uncertain"]:
                raise HomeAssistantError(
                    "Fixture provider failed after applying the action"
                )

    schema = probatio.Schema({probatio.Required("entity_id"): cv.entity_ids})
    for service in ("turn_on", "turn_off", "toggle"):
        hass.services.async_register(domain, service, change, schema=schema)
    await hass.async_block_till_done()
    return {"area_id": area.id, "entities": ids}
