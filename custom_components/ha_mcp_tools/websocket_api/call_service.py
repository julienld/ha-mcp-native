"""WebSocket compatibility envelope around the canonical Core service call."""

from typing import Any

from homeassistant.core import HomeAssistant

from ..core_call_service import (
    _call_service_transition,
    _confirmable_entity_ids,
    _guard_call_service_target,
    _match_immediate,
    _post_state,
    _register_transition_waiter,
    _values_differ,
    call_service,
)


async def _call_service_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    values = {
        key: value
        for key, value in msg.items()
        if key not in {"id", "type", "ha_mcp_context"}
    }
    return {
        "result": await call_service(hass, **values, context=msg.get("ha_mcp_context"))
    }


def _do_call_service(
    hass: HomeAssistant, params: dict[str, Any], *, result: dict[str, Any]
) -> dict[str, Any]:
    return result


__all__ = [
    "_call_service_transition",
    "_confirmable_entity_ids",
    "_guard_call_service_target",
    "_match_immediate",
    "_post_state",
    "_register_transition_waiter",
    "_values_differ",
]
