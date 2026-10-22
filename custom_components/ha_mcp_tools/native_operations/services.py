"""Observe native service dispatch without losing REST's changed-state body."""

from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback


async def call_and_observe(
    hass: HomeAssistant,
    message: dict[str, Any],
    prepare: Callable[..., Awaitable[dict[str, Any]]],
    finish: Callable[..., dict[str, Any]],
    *,
    include_changed_states: bool = False,
) -> dict[str, Any]:
    """Reuse call_service prep's guards/confirmation and retain all changed states.

    Core 2026.10.2's APIDomainServicesView selects state_changed events by the
    call's Context. This observer uses that same ownership rule. The existing
    component prep still owns registration-before-fire confirmation, dispatch,
    timeout and its listener; no HTTP/WS request or handler is constructed.
    """
    if not include_changed_states:
        return finish(hass, message, **await prepare(hass, message))
    changed: list[dict[str, Any]] = []
    context = message["ha_mcp_context"]

    @callback
    def record(event: Event) -> None:
        if (
            event.context == context
            and (state := event.data.get("new_state")) is not None
        ):
            changed.append(state.as_dict())

    unsubscribe = hass.bus.async_listen(EVENT_STATE_CHANGED, record)
    try:
        extra = await prepare(hass, message)
        result = finish(hass, message, **extra)
        return {**result, "changed_states": changed}
    finally:
        unsubscribe()
