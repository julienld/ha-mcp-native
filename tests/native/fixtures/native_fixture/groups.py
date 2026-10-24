"""Observe the actual old-style Core Group and its current StateMachine value."""

from typing import Any

from homeassistant.components.group.const import DATA_COMPONENT
from homeassistant.core import HomeAssistant

ENTITY_ID = "group.native_contract_group"


def describe_group(hass: HomeAssistant) -> dict[str, Any]:
    component = hass.data.get(DATA_COMPONENT)
    owner = component.get_entity(ENTITY_ID) if component is not None else None
    state = hass.states.get(ENTITY_ID)
    return {
        "entity_id": ENTITY_ID,
        "state": state.state if state is not None else None,
        "user_id": state.context.user_id if state is not None else None,
        "owner": {
            "type": type(owner).__name__,
            "name": owner.name,
            "icon": owner.icon,
            "members": list(owner.tracking),
            "all": owner.mode is all,
        }
        if owner is not None
        else None,
    }
