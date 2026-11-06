"""Reuse installed Core command validators without invoking their handlers."""

from typing import Any

from homeassistant.core import HomeAssistant

from ha_mcp.errors import create_validation_error
from ha_mcp.tool_api import ToolFailure, UnsupportedOperation

from ..helper_collections import _INVALID_ERRORS


def _validated_payload(
    hass: HomeAssistant, command: str, payload: dict[str, Any]
) -> dict[str, Any]:
    if {"id", "type"}.intersection(payload):
        raise UnsupportedOperation("registry.payload.envelope")
    registration = (hass.data.get("websocket_api") or {}).get(command)
    if not registration:
        raise UnsupportedOperation(command + ".validator")
    schema = registration[1]
    if schema is False:
        # Core marks type-only commands False; it still rejects extra keys.
        # Reuse its public base schema instead of invoking a WS handler.
        from homeassistant.components.websocket_api.messages import (
            BASE_COMMAND_MESSAGE_SCHEMA,
        )

        schema = BASE_COMMAND_MESSAGE_SCHEMA.extend({"type": command})
    if not callable(schema):
        raise UnsupportedOperation(command + ".validator")
    try:
        validated: dict[str, Any] = schema({"id": 1, "type": command, **payload})
    except _INVALID_ERRORS as error:
        data = create_validation_error(str(error))
        raise ToolFailure(
            data["error"]["code"],
            data["error"]["message"],
            data=data,
            status="not_started",
        ) from error
    return {key: value for key, value in validated.items() if key not in {"id", "type"}}
