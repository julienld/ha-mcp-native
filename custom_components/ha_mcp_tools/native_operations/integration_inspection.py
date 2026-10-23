"""Read integration-owned log, KNX and diagnostics data without any HTTP request."""

import asyncio
import json
from typing import Any

from homeassistant.core import HomeAssistant

from ha_mcp.errors import HomeAssistantAPIError, HomeAssistantConnectionError

from ..optional_integrations import loaded_integration_module


def logger_levels(hass: HomeAssistant) -> list[dict[str, Any]] | None:
    """Core logger domains and levels, without a transport envelope."""
    from homeassistant.components.logger.helpers import DATA_LOGGER, get_logger
    from homeassistant.config_entries import DISCOVERY_SOURCES
    from homeassistant.setup import async_get_loaded_integrations

    owner = hass.data.get(DATA_LOGGER)
    if owner is None:
        return None
    domains = set(async_get_loaded_integrations(hass))
    domains.update(
        flow["handler"]
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"].get("source") in DISCOVERY_SOURCES
    )
    domains.update(owner.settings.async_get_integration_domains())
    return [
        {
            "domain": domain,
            "level": get_logger(
                f"homeassistant.components.{domain}"
            ).getEffectiveLevel(),
        }
        for domain in domains
    ]


async def log_levels(hass: HomeAssistant, arguments: dict[str, Any]) -> dict[str, Any]:
    rows = logger_levels(hass)
    if rows is None:
        return {"success": False, "error": "Logger integration not loaded"}
    return {"success": True, "result": rows}


async def knx_project(hass: HomeAssistant, arguments: dict[str, Any]) -> dict[str, Any]:
    if "knx" not in hass.config.components:
        return {"success": False, "error": "KNX integration not loaded."}
    KNX_MODULE_KEY = loaded_integration_module(hass, "knx.const").KNX_MODULE_KEY
    owner = hass.data.get(KNX_MODULE_KEY)
    if owner is None:
        return {"success": False, "error": "KNX integration not loaded."}
    return {"success": True, "result": await owner.project.get_knxproject()}


async def diagnostics(hass: HomeAssistant, arguments: dict[str, Any]) -> Any:
    try:
        async with asyncio.timeout(float(arguments.get("timeout", 60))):
            return await _diagnostics_value(hass, arguments)
    except TimeoutError as error:
        raise HomeAssistantConnectionError("Diagnostics timeout") from error


async def _diagnostics_value(hass: HomeAssistant, arguments: dict[str, Any]) -> Any:
    from homeassistant.components import diagnostics as owner
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import issue_registry as ir

    entry_id, device_id = arguments["entry_id"], arguments.get("device_id") or None
    entry = hass.config_entries.async_get_entry(entry_id)
    platforms = hass.data.get(owner._DIAGNOSTICS_DATA)
    info = (
        platforms.platforms.get(entry.domain)
        if platforms is not None and entry is not None
        else None
    )
    if info is None:
        raise HomeAssistantAPIError("Diagnostics not available", status_code=404)
    issues = [
        issue.to_json()
        for key, issue in ir.async_get(hass).issues.items()
        if key[0] == entry.domain
    ]
    filename = f"{entry.domain}-{entry.entry_id}"
    if device_id is not None:
        device = dr.async_get(hass).async_get(
            device_id, include_composite_devices=False
        )
        if (
            device is None
            or device.config_entry_id != entry.entry_id
            or info.device_diagnostics is None
        ):
            raise HomeAssistantAPIError(
                "Device diagnostics not available", status_code=404
            )
        data = await info.device_diagnostics(hass, entry, device)
        filename += f"-{device.name}-{device.id}"
    else:
        if info.config_entry_diagnostics is None:
            raise HomeAssistantAPIError("Diagnostics not available", status_code=404)
        data = await info.config_entry_diagnostics(hass, entry)
        filename = f"config_entry-{filename}"
    # Reuse Core's complete diagnostics artifact formatter. It constructs a
    # response value; no request, network connection or handler is fabricated.
    response = await owner._async_get_json_file_response(
        hass, data, issues, filename, entry.domain, entry_id, device_id
    )
    if response.status != 200:
        raise HomeAssistantAPIError(
            "Diagnostics serialization failed", status_code=response.status
        )
    return json.loads(response.text)


DIRECT_OPERATIONS = {
    "integrations.log_levels": (log_levels, False),
    "integrations.knx_project": (knx_project, False),
    "integrations.diagnostics": (diagnostics, False),
}
