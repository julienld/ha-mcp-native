"""Entity-independent reads through Home Assistant's Z-Wave JS API."""

from __future__ import annotations

from typing import Any

from ha_mcp.backends.legacy import legacy_errors_sync
from ha_mcp.runtime import radio_parameters as _values
from ha_mcp.runtime.radio_parameters import read as _shared_read

from ...client.rest_client import (
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ...errors import ErrorCode, create_error_response
from ..helpers import exception_to_structured_error, raise_tool_error
from .base import ws_call

_no_raw_value = legacy_errors_sync(_values._no_raw_value)


def _raw_read_error(reply: dict[str, Any], context: dict[str, Any]) -> None:
    # HA reports unknown_error when the raw client asserts on an empty result;
    # the code can also mean another upstream failure, so preserve its details.
    if reply.get("error_code") == "unknown_error":
        _no_raw_value(context, reply.get("error"))


async def _read_raw(client: Any, device_id: str, property_: int) -> Any:
    context: dict[str, Any] = {
        "radio": "zwave",
        "action": "get_config_param",
        "ws_type": "zwave_js/get_raw_config_parameter",
        "device_id": device_id,
        "property": property_,
        "refresh_requested": True,
    }
    try:
        raw = await ws_call(
            client,
            context["ws_type"],
            device_id=device_id,
            property=property_,
            context=context,
            on_error=_raw_read_error,
        )
    except (HomeAssistantCommandTimeout, HomeAssistantConnectionError) as error:
        # The REST bridge wraps its command deadline in a connection error.
        # A socket failure without that cause must remain a transport failure.
        if isinstance(error, HomeAssistantCommandTimeout) or isinstance(
            error.__cause__, HomeAssistantCommandTimeout
        ):
            raise_tool_error(
                create_error_response(
                    ErrorCode.TIMEOUT_WEBSOCKET,
                    "Device parameter read did not answer before the command timeout; "
                    "no cached fallback was used. The Get may still be queued until the node wakes",
                    context=context,
                    suggestions=[
                        "Wake/check the device and allow any queued Get to finish before retrying"
                    ],
                )
            )
        exception_to_structured_error(error, context=context)
    if not isinstance(raw, dict) or raw.get("value") is None:
        _no_raw_value(context)
    return raw["value"]


class RemoteParameters:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def parameters(self, device_id: str) -> Any:
        return await ws_call(
            self.client,
            "zwave_js/get_config_parameters",
            device_id=device_id,
            context={"device_id": device_id},
        )

    async def raw(self, device_id: str, property_: int) -> Any:
        return await _read_raw(self.client, device_id, property_)


async def read(client: Any, action: str, args: dict[str, Any]) -> dict[str, Any]:
    from ha_mcp.backends.legacy import legacy_errors

    result: dict[str, Any] = await legacy_errors(_shared_read)(
        RemoteParameters(client), action, args
    )
    return result


__all__ = [
    "read",
]
