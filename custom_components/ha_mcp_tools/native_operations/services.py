"""Observe native service dispatch without losing REST's changed-state body."""

from typing import Any

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback

from ..core_call_service import call_service


async def call_and_observe(
    hass: HomeAssistant,
    parameters: dict[str, Any],
    *,
    context: Any,
    include_changed_states: bool = False,
) -> dict[str, Any]:
    """Observe the canonical Core call and retain all actor-correlated states.

    Core 2026.10.2's APIDomainServicesView selects state_changed events by the
    call's Context. This observer uses that same ownership rule. The existing
    service owner still owns registration-before-fire confirmation, dispatch,
    timeout and its listener; no HTTP/WS request or handler is constructed.
    """
    if not include_changed_states:
        return await call_service(hass, **parameters, context=context)
    changed: list[dict[str, Any]] = []

    @callback
    def record(event: Event) -> None:
        if (
            event.context == context
            and (state := event.data.get("new_state")) is not None
        ):
            changed.append(state.as_dict())

    unsubscribe = hass.bus.async_listen(EVENT_STATE_CHANGED, record)
    try:
        result = await call_service(hass, **parameters, context=context)
        return {**result, "changed_states": changed}
    finally:
        unsubscribe()
