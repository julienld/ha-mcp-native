"""WebSocket compatibility facade for Core registry snapshots."""

from typing import Any

from homeassistant.core import HomeAssistant

from ..core_registries import (
    _category_registry,
    registry_snapshot,
)


def _do_registries(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    return registry_snapshot(
        hass, params.get("registries") or [], params.get("category_scopes")
    )


__all__ = [
    "_category_registry",
]
