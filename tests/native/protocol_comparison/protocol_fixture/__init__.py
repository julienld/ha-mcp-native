"""Test-only integration, copied into the disposable continuity containers."""

from typing import Any


async def async_setup(hass: Any, config: dict[str, Any]) -> bool:
    from .runtime import setup

    return await setup(hass)
