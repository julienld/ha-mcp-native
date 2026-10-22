"""Observe Core states after acknowledged changes without replaying mutations."""

import asyncio
from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant, callback

from ha_mcp.tool_api import UnsupportedOperation


async def _wait_for(
    hass: HomeAssistant,
    sample: Callable[[], Any],
    matches: Callable[[str], bool],
    timeout: float,
) -> Any:
    wake = asyncio.Event()

    @callback
    def changed(event: Any) -> None:
        if matches(event.data.get("entity_id", "")):
            wake.set()

    unsubscribe = hass.bus.async_listen("state_changed", changed)
    try:
        async with asyncio.timeout(timeout):
            while True:
                wake.clear()
                current = sample()
                if current:
                    return current
                try:
                    await asyncio.wait_for(wake.wait(), timeout=0.3)
                except TimeoutError:
                    pass
    except TimeoutError:
        return None
    finally:
        unsubscribe()


async def wait_removed(hass: HomeAssistant, arguments: dict[str, Any]) -> bool:
    """Observe the state owner after deletion, never reissue the deletion."""
    return await _wait_presence(hass, arguments["entity_id"], present=False)


async def wait_registered(hass: HomeAssistant, arguments: dict[str, Any]) -> bool:
    """A registry entry alone does not prove its state is queryable yet."""
    return await _wait_presence(hass, arguments["entity_id"], present=True)


async def _wait_presence(hass: HomeAssistant, entity_id: str, *, present: bool) -> bool:
    return bool(
        await _wait_for(
            hass,
            lambda: (hass.states.get(entity_id) is not None) == present,
            lambda changed: changed == entity_id,
            10,
        )
    )


async def wait_state(hass: HomeAssistant, arguments: dict[str, Any]) -> Any:
    entity_id, expected = arguments["entity_id"], arguments["expected_state"]
    if expected not in {"on", "off"}:
        raise UnsupportedOperation("entities.state.wait.expected_state")

    def sample() -> Any:
        state = hass.states.get(entity_id)
        return (
            state.as_dict() if state is not None and state.state == expected else None
        )

    return await _wait_for(hass, sample, lambda changed: changed == entity_id, 10)


async def wait_automation_entity(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> str | None:
    key = arguments["storage_key"]
    budget = max(0.0, min(6.0, float(arguments.get("timeout", 6))))

    def sample() -> str | None:
        return next(
            (
                state.entity_id
                for state in hass.states.async_all()
                if state.entity_id.startswith("automation.")
                and state.attributes.get("id") == key
            ),
            None,
        )

    result: str | None = await _wait_for(
        hass, sample, lambda changed: changed.startswith("automation."), budget
    )
    return result
