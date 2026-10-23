"""Find actual Core config owners without invoking HTTP handlers or requests."""

from __future__ import annotations

import inspect
from importlib import import_module
from typing import Any

from homeassistant.core import HomeAssistant

from ha_mcp.tool_api import UnsupportedOperation


def config_view(hass: HomeAssistant, domain: str) -> Any:
    """Retain Core's registered view instance, file reader and mutation lock.

    Core's request_handler_factory closes over its view but exposes no view
    registry. Match the fixed Core route before inspecting that one closure.
    Changed owner or hosting contracts fail explicitly instead of constructing
    another view with an independent mutation lock.
    """
    if domain not in {"automation", "script", "scene"}:
        raise UnsupportedOperation(f"config.{domain}.read")
    path = f"/api/config/{domain}/config/{{config_key}}"
    name = f"api:config:{domain}:config"
    try:
        owner_type = getattr(
            import_module(f"homeassistant.components.config.{domain}"),
            f"Edit{domain.title()}ConfigView",
        )
    except (ImportError, AttributeError) as error:
        raise UnsupportedOperation(f"config.{domain}.read") from error
    for route in hass.http.app.router.routes():
        if route.method != "GET" or route.resource.canonical != path:
            continue
        try:
            owner = inspect.getclosurevars(route.handler).nonlocals.get("view")
        except TypeError:
            continue
        if isinstance(owner, owner_type) and owner.name == name and owner.url == path:
            return owner
    raise UnsupportedOperation(f"config.{domain}.read")


async def read_config_item(
    hass: HomeAssistant, domain: str, storage_key: str
) -> dict[str, Any] | None:
    """Read current persisted content with the same lock/reader as Core's API."""
    owner = config_view(hass, domain)
    async with owner.mutation_lock:
        current = await owner.read_config(hass)
        value: dict[str, Any] | None = owner._get_value(hass, current, storage_key)
    return value
