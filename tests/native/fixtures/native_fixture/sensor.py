"""A real integration-owned sensor for native registry and recovery checks."""

import json
from functools import partial
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.json import json_bytes

DOMAIN = "native_fixture"
LEGACY_STATE_ID = "sensor.native_state_history"


class NativeFixtureSensor(SensorEntity):
    _attr_name = "Native migration sensor"
    _attr_should_poll = False
    _attr_native_value = 1.25
    _attr_native_unit_of_measurement = "kWh"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, entry_id: str) -> None:
        self._attr_unique_id = f"{entry_id}:sensor"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}:native-device")},
            name="Native migration device",
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([NativeFixtureSensor(entry.entry_id)])


def describe_sensor(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    """Resolve the actual entity ID by integration identity, including renames."""
    from homeassistant.components.homeassistant.exposed_entities import (
        DATA_EXPOSED_ENTITIES,
    )

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry_id}:sensor")
    entry = registry.async_get(entity_id) if entity_id is not None else None
    state = hass.states.get(entity_id) if entity_id is not None else None
    legacy = hass.data[DATA_EXPOSED_ENTITIES].entities.get(LEGACY_STATE_ID)
    legacy_state = hass.states.get(LEGACY_STATE_ID)
    result: dict[str, Any] = json.loads(
        json_bytes(
            {
                "entity_id": entity_id,
                "registry_id": entry.id if entry is not None else None,
                "registry_entry": entry.extended_dict if entry is not None else None,
                "state": state.as_dict() if state is not None else None,
                "legacy_entity_id": LEGACY_STATE_ID,
                "legacy_preferences": legacy.to_json() if legacy is not None else None,
                "legacy_state": legacy_state.as_dict()
                if legacy_state is not None
                else None,
            }
        )
    )
    return result


async def seed_entity_state_history(
    hass: HomeAssistant, manager: Any
) -> dict[str, str]:
    """Create an authentic historical state snapshot without a registry entry."""
    assert er.async_get(hass).async_get(LEGACY_STATE_ID) is None
    hass.states.async_set(LEGACY_STATE_ID, "saved", {"fixture": "before"})
    state = hass.states.get(LEGACY_STATE_ID)
    assert state is not None
    saved = json.loads(json_bytes(state.as_dict()))
    capture = partial(
        manager.snapshots._store().capture,
        "entity",
        LEGACY_STATE_ID,
        saved,
        "native_fixture",
        force=True,
    )
    path = await hass.async_add_executor_job(capture)
    assert path is not None
    invalid = await hass.async_add_executor_job(
        partial(
            manager.snapshots._store().capture,
            "entity",
            LEGACY_STATE_ID,
            {"state": "x" * 256, "attributes": {}},
            "native_fixture",
            force=True,
        )
    )
    assert invalid is not None
    hass.states.async_set(LEGACY_STATE_ID, "changed", {"fixture": "after"})
    return {
        "entity_id": LEGACY_STATE_ID,
        "snapshot": path.name,
        "invalid_snapshot": invalid.name,
    }
