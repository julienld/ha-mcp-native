"""WebSocket compatibility projection over Core service readers."""

from typing import Any

from homeassistant.core import HomeAssistant

from ..core_services import (
    _fetch_service_descriptions,
    _fetch_service_translations,
    load_service_descriptions,
    project_service_catalog,
    reference_data,
)


async def _services_list_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    return await load_service_descriptions(hass, msg.get("language") or "en")


def _do_services_list(
    hass: HomeAssistant, params: dict[str, Any], **details: Any
) -> dict[str, Any]:
    return project_service_catalog(hass, params, **details)


def _do_reference_data(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    return reference_data(hass, params.get("include_states", True))


__all__ = [
    "_fetch_service_descriptions",
    "_fetch_service_translations",
]
