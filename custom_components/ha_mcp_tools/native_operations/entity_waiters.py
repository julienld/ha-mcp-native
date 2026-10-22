"""Observe Core state removal without a transport or a repeated mutation."""

import asyncio
from typing import Any

from homeassistant.core import HomeAssistant, callback


async def wait_removed(hass: HomeAssistant, arguments: dict[str, Any]) -> bool:
    """Observe the state owner after acknowledged deletion, never reissue it."""
    return await _wait_presence(hass, arguments["entity_id"], present=False)


async def wait_registered(hass: HomeAssistant, arguments: dict[str, Any]) -> bool:
    """A registry entry alone does not prove its state is queryable yet."""
    return await _wait_presence(hass, arguments["entity_id"], present=True)


async def _wait_presence(hass: HomeAssistant, entity_id: str, *, present: bool) -> bool:
    wake = asyncio.Event()

    @callback
    def changed(event: Any) -> None:
        if event.data.get("entity_id") == entity_id:
            wake.set()

    unsubscribe = hass.bus.async_listen("state_changed", changed)
    try:
        async with asyncio.timeout(10):
            while (hass.states.get(entity_id) is not None) != present:
                wake.clear()
                try:
                    await asyncio.wait_for(wake.wait(), timeout=0.3)
                except TimeoutError:
                    pass
        return True
    except TimeoutError:
        return False
    finally:
        unsubscribe()
