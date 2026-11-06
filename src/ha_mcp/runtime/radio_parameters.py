"""Shared Z-Wave parameter selection, validation and response projection."""

from typing import Any, NoReturn

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure
from .radio_values import ok


def _invalid(message: str) -> NoReturn:
    raise_failure(
        create_error_response(ErrorCode.VALIDATION_INVALID_PARAMETER, message)
    )


def _integer(args: dict[str, Any], name: str, minimum: int, maximum: int) -> None:
    value = args.get(name)
    if type(value) is not int or not minimum <= value <= maximum:
        _invalid(f"params.{name} must be an integer between {minimum} and {maximum}")


def _validate(action: str, args: dict[str, Any]) -> bool:
    """Validate read arguments before traffic; return whether refresh was requested."""
    device_id = args["device_id"]
    if not isinstance(device_id, str) or not device_id.strip():
        _invalid("device_id must be a non-empty Home Assistant device ID")
    refresh = args.get("refresh", False)
    if not isinstance(refresh, bool):
        _invalid("params.refresh must be a boolean")
    if action == "get_config_params" and refresh:
        _invalid(
            "get_config_params is cache-only; use get_config_param to refresh one parameter"
        )
    if action == "get_config_param":
        _integer(args, "property", 0, 65535)
        if "endpoint" in args:
            _integer(args, "endpoint", 0, 127)
        if args.get("property_key") is not None:
            _integer(args, "property_key", 1, 0xFFFFFFFF)
        if refresh and (
            args.get("endpoint", 0) != 0 or args.get("property_key") is not None
        ):
            _invalid(
                "HA's raw-read API cannot refresh endpoint or partial parameters; use a cached read"
            )

    return refresh


def _no_raw_value(context: dict[str, Any], details: str | None = None) -> NoReturn:
    message = (
        "Device parameter read failed without a value (HA unknown_error); no cached fallback was used"
        if context.get("ha_error_code") == "unknown_error"
        else "Device parameter read returned no value; no cached fallback was used"
    )
    raise_failure(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            message,
            details=details,
            context=context,
            suggestions=[
                "Verify that the parameter number is supported by this device",
                "Check node availability or wake a sleeping device before retrying",
                "Inspect Home Assistant logs if the failure persists; unknown_error does not identify the cause",
            ],
        )
    )


async def read(client: Any, action: str, args: dict[str, Any]) -> dict[str, Any]:
    """Read cached values, or explicitly request one root parameter from the device."""
    refresh = _validate(action, args)
    device_id = args["device_id"]

    parameters = await client.parameters(device_id)
    if not isinstance(parameters, dict):
        raise_failure(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "HA returned an invalid configuration-parameter response",
                context={"device_id": device_id},
            )
        )
    if action == "get_config_params":
        return ok(
            "zwave",
            action,
            device_id=device_id,
            parameters=parameters,
            source="cache",
            refresh_requested=False,
        )

    property_ = args["property"]
    endpoint = args.get("endpoint", 0)
    property_key = args.get("property_key")
    matches = [
        {"value_id": value_id, **parameter}
        for value_id, parameter in parameters.items()
        if parameter.get("property") == property_
        and parameter.get("endpoint") == endpoint
        and parameter.get("property_key") == property_key
    ]
    if len(matches) > 1:
        raise_failure(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "HA returned multiple matching configuration parameters",
                context={"device_id": device_id, "property": property_},
            )
        )
    if not refresh and not matches:
        raise_failure(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                "Configuration parameter not found in the Z-Wave JS cache",
                context={
                    "device_id": device_id,
                    "property": property_,
                    "endpoint": endpoint,
                    "property_key": property_key,
                },
                suggestions=[
                    "Use get_config_params to inspect available endpoints and property_key bit masks",
                    "For a full root parameter, request params.refresh=True to query the device",
                ],
            )
        )

    parameter = (
        matches[0]
        if matches
        else {
            "property": property_,
            "endpoint": 0,
            "property_key": None,
            "metadata": None,
        }
    )
    if refresh:
        parameter = {
            **parameter,
            "value": await client.raw(device_id, property_),
        }

    result = ok(
        "zwave",
        action,
        device_id=device_id,
        parameter=parameter,
        source="device_request" if refresh else "cache",
        refresh_requested=refresh,
    )
    if parameter.get("value") is None:
        result["warnings"] = [
            "Z-Wave JS has no cached value for this parameter; the device was not queried"
        ]
    if refresh and matches:
        result["metadata_source"] = "cache"
    return result
