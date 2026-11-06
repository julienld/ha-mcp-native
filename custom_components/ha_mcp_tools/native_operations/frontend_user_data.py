"""Pin an already resolved Frontend user store without triggering a load."""

from dataclasses import dataclass, field
from typing import Any

from ha_mcp.tool_api import ToolFailure, UnsupportedOperation


@dataclass(frozen=True, slots=True)
class ThemeStorePin:
    user_id: str
    future: Any = field(repr=False)
    store: Any = field(repr=False)


def cached_theme_store(hass: Any, user_id: str) -> ThemeStorePin:
    from homeassistant.components.frontend.storage import DATA_STORAGE, UserStore

    future = hass.data.get(DATA_STORAGE, {}).get(user_id)
    if future is None or not future.done():
        raise ToolFailure(
            "SERVICE_UNAVAILABLE",
            "The engine account's frontend preferences are not loaded. Read-only inspection does not initialize or migrate a Core user store.",
            status="not_started",
        )
    if future.cancelled() or future.exception() is not None:
        raise ToolFailure(
            "SERVICE_UNAVAILABLE",
            "The Core user store did not load successfully.",
            status="not_started",
        )
    store = future.result()
    if type(store) is not UserStore:
        raise UnsupportedOperation("frontend.engine_theme.user_store_owner")
    return ThemeStorePin(user_id, future, store)


def check_theme_store(hass: Any, pin: ThemeStorePin) -> None:
    current = cached_theme_store(hass, pin.user_id)
    if current.future is not pin.future or current.store is not pin.store:
        raise ToolFailure(
            "CONFIG_CONFLICT",
            "The Core engine user store changed before dispatch.",
            status="not_started",
        )
