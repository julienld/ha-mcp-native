"""Unified radio management tool: ``ha_manage_radio``.

One multi-modal tool for managing every radio Home Assistant speaks — Z-Wave
(zwave_js), Zigbee (ZHA), Matter, and Thread/OTBR. Read-only diagnostics also
live in ``ha_get_device`` and ``ha_get_system_health`` (next to the existing
Z-Wave/Zigbee surfaces); they are mirrored here so they are at hand mid-
management. This tool additionally performs writes those read tools do not, so
it is not a subset of either.

Per-radio logic lives in ``radio/<name>.py``; this module is the dispatcher.
"""

from __future__ import annotations

import logging
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import legacy_errors
from ..errors import ErrorCode, create_error_response
from ..runtime.radio_tools import RadioFacade
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .radio import matter as matter_handler
from .radio import thread as thread_handler
from .radio import zigbee as zigbee_handler
from .radio import zwave as zwave_handler
from .tool_hints import write_hints
from .util_helpers import is_connection_error_message

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {
    "zwave": zwave_handler,
    "zigbee": zigbee_handler,
    "matter": matter_handler,
    "thread": thread_handler,
}


class RadioTools:
    """``ha_manage_radio`` and its dispatch helpers."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.radios = self

    async def resolve_entity_device(self, entity_id: str) -> str:
        """Resolve an entity_id to its device_id via the entity registry.

        Uses HA's targeted ``config/entity_registry/get`` (single-entity
        lookup) rather than pulling the whole registry list to find one row.
        """
        result = await self._client.send_websocket_message(
            {"type": "config/entity_registry/get", "entity_id": entity_id}
        )
        if result.get("success"):
            device_id = (result.get("result") or {}).get("device_id")
            if device_id:
                return str(device_id)
        else:
            error = str(result.get("error", ""))
            if is_connection_error_message(error):
                # A transport drop is not evidence the entity is missing.
                raise_tool_error(
                    create_error_response(
                        ErrorCode.CONNECTION_FAILED,
                        f"Could not resolve entity '{entity_id}': {error}",
                        context={"entity_id": entity_id},
                        suggestions=[
                            "Home Assistant may be restarting or unreachable — retry shortly",
                        ],
                    )
                )
        raise_tool_error(
            create_error_response(
                ErrorCode.ENTITY_NOT_FOUND,
                f"Entity '{entity_id}' not found or has no associated device",
                context={"entity_id": entity_id},
                suggestions=["Use ha_search() to find a valid entity_id"],
            )
        )
        raise AssertionError  # py/mixed-returns terminal: raise_tool_error is NoReturn

    async def handle(
        self, radio: str, action: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            result: dict[str, Any] = await HANDLERS[radio].handle(
                self._client, action, args
            )
            return result
        except ToolError:
            raise
        except Exception as error:
            exception_to_structured_error(
                error, context={"radio": radio, "action": action}
            )
            raise

    _resolve_entity_device = resolve_entity_device
    ha_manage_radio = tool(
        name="ha_manage_radio",
        tags={"Radio Management", "Z-Wave", "Zigbee", "Matter", "Thread"},
        annotations=write_hints(
            "Manage Radios (Z-Wave / Zigbee / Matter / Thread)",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )(log_tool_usage(legacy_errors(RadioFacade.ha_manage_radio)))


def register_radio_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register the radio management tool (auto-discovered by the registry)."""
    register_tool_methods(mcp, RadioTools(client))
