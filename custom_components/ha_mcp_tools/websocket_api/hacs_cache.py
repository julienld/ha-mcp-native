"""Register cached HACS/Marketplace reads without loading the native runtime."""

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api


def async_register_hacs_cache(hass: Any) -> None:
    """Register on the shared component surface, for either entry topology."""

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "ha_mcp_tools/hacs_cache",
            vol.Required("repository_id"): str,
        }
    )
    @websocket_api.require_admin
    @websocket_api.async_response
    async def cache(hass: Any, connection: Any, msg: dict[str, Any]) -> None:
        from ha_mcp.tool_api import ToolFailure

        from ..native_operations.hacs_cache import cached_store_info

        try:
            result = await cached_store_info(
                hass, connection.user.id, msg["repository_id"]
            )
        except (ToolFailure, PermissionError) as error:
            connection.send_error(
                msg["id"], getattr(error, "code", "unauthorized"), str(error)
            )
        else:
            connection.send_result(msg["id"], result)

    websocket_api.async_register_command(hass, cache)
