"""Existing strict registry acquisitions used by remote reconfiguration."""

import logging
from typing import Any

from ha_mcp.errors import (
    HomeAssistantAPIError,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.component_devices import fetch_device_list_via_component
from ha_mcp.tools.component_registry_lookup import (
    fetch_entities_for_config_entry_via_component,
)

logger = logging.getLogger(__name__)


async def _entry_entity_rows(client: Any, entry_id: str) -> list[dict[str, Any]]:
    """Entity-registry rows belonging to one config entry.

    Prefers the custom component's ``registry_lookup``, which filters
    server-side, so the common path no longer pulls the whole entity registry
    across the wire once per identity collection. Falls back to the legacy
    whole-registry dump when the component is absent or older.
    """
    rows = await fetch_entities_for_config_entry_via_component(client, entry_id)
    if rows is not None:
        return [row for row in rows if isinstance(row, dict)]
    all_rows = await _registry_rows(
        client.list_entity_registry, "list_entity_registry", entry_id
    )
    return [row for row in all_rows if row.get("config_entry_id") == entry_id]


async def _device_registry_rows(client: Any, entry_id: str) -> list[dict[str, Any]]:
    """Every device-registry row, via the component when it offers the read."""
    payload = await fetch_device_list_via_component(client)
    if payload is not None:
        return [row for row in payload["devices"] if isinstance(row, dict)]
    return await _registry_rows(
        client.list_device_registry, "list_device_registry", entry_id
    )


async def _registry_rows(
    reader: Any,
    method_name: str,
    entry_id: str,
) -> list[dict[str, Any]]:
    """Read one registry, translating transport failures for fail-closed callers.

    Never degrades to "registry unavailable": a registry this reconfigure
    cannot read is a reason to stop, not to skip the identity and duplicate
    checks that depend on it. Callers that can tolerate the failure catch it.
    """
    try:
        result: Any = await reader()
    except (
        ConnectionError,
        OSError,
        TimeoutError,
        HomeAssistantConnectionError,
        HomeAssistantCommandError,
        HomeAssistantCommandTimeout,
    ) as err:
        logger.warning(
            "Registry read %s failed for %s (%s): %s",
            method_name,
            entry_id,
            type(err).__name__,
            err,
        )
        raise (
            err
            if isinstance(err, HomeAssistantConnectionError)
            else HomeAssistantConnectionError(
                f"{method_name} transport unavailable for {entry_id}"
            )
        ) from err
    if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
        raise HomeAssistantAPIError(
            f"Unexpected response from {method_name} for {entry_id}",
            status_code=500,
        )
    return result
