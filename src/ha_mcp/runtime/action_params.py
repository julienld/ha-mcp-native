"""Shared service input validation; Core still owns service field schemas."""

from typing import Any

from ha_mcp.errors import (
    create_validation_error,
)
from ha_mcp.tools.coercion import parse_json_param, parse_string_list_param

from .errors import raise_failure


def _parse_json_dict_param(
    data: str | dict[str, Any] | None,
    *,
    type_error_message: str,
) -> dict[str, Any] | None:
    if data is None:
        return None
    raw: Any = None
    try:
        raw = parse_json_param(data, "data")
    except ValueError as e:
        raise_failure(
            create_validation_error(
                f"Invalid data parameter: {e}",
                parameter="data",
                invalid_json=True,
            )
        )
    if raw is not None and not isinstance(raw, dict):
        raise_failure(
            create_validation_error(
                type_error_message,
                parameter="data",
                details=f"Received type: {type(raw).__name__}",
            )
        )
    return raw if isinstance(raw, dict) else None


class ActionParameters:
    @staticmethod
    def _parse_service_data(
        data: str | dict[str, Any] | None,
        entity_id: str | None,
    ) -> dict[str, Any]:
        """Parse and validate the data parameter into a service_data dict."""
        service_data: dict[str, Any] = (
            _parse_json_dict_param(
                data, type_error_message="Data parameter must be a JSON object"
            )
            or {}
        )
        if entity_id:
            service_data["entity_id"] = entity_id
        return service_data

    @staticmethod
    def _validate_service_call_params(
        domain: str | None, service: str | None
    ) -> tuple[str, str]:
        """Validate service-mode params and return the (domain, service) pair.

        Raises a structured ToolError when domain/service are missing (the caller
        likely wants the ws_command escape hatch), when the domain targets the
        reserved ha_mcp_tools namespace, or when backup controls block the service.
        """
        if not domain or not service:
            raise_failure(
                create_validation_error(
                    "domain and service are required for a service call. To send "
                    "a raw WebSocket command instead, pass ws_command.",
                    parameter="domain" if not domain else "service",
                )
            )
        # ha_mcp_tools.* services are restricted to the ha-mcp server's dedicated
        # wrappers (which inject the required caller token); forwarding to that
        # domain would bypass them. is_component_domain normalises case because
        # HA core's service registry lowercases the domain on fallback lookup.
        if domain.strip().lower() == "ha_mcp_tools":
            raise_failure(
                create_validation_error(
                    (
                        "ha_call_service cannot invoke services in the "
                        "'ha_mcp_tools' domain. Use the dedicated MCP tool "
                        "instead: ha_list_files, ha_read_file, ha_write_file, "
                        "ha_delete_file, or ha_config_set_yaml."
                    ),
                    parameter="domain",
                )
            )
        return domain, service

    @staticmethod
    def _reject_incompatible_ws_params(
        entity_id: str | None,
        return_response: bool,
        verbose: bool,
        result_fields: str | list[str] | None,
        result_attribute_keys: str | list[str] | None,
    ) -> None:
        """Reject service-mode-only params when the ws_command escape hatch is used.

        These shape a registered-service call and have no meaning for a raw
        WebSocket command; silently ignoring them would be a confusing no-op, so
        (mirroring the domain/service "not both" guard) they must be omitted.
        """
        offenders = [
            name
            for name, is_set in (
                ("entity_id", entity_id is not None),
                ("return_response", return_response),
                ("verbose", verbose),
                ("result_fields", result_fields is not None),
                ("result_attribute_keys", result_attribute_keys is not None),
            )
            if is_set
        ]
        if offenders:
            raise_failure(
                create_validation_error(
                    "These parameters apply only to service calls and must be "
                    f"omitted when ws_command is set: {', '.join(offenders)}.",
                    parameter="ws_command",
                )
            )

    @staticmethod
    def _parse_result_projection_params(
        result_fields: str | list[str] | None,
        result_attribute_keys: str | list[str] | None,
    ) -> tuple[list[str] | None, list[str] | None]:
        """Parse and validate result_fields / result_attribute_keys into lists.

        Raises a structured VALIDATION_INVALID_PARAMETER ToolError on malformed
        input for either parameter.
        """
        try:
            parsed_result_fields = parse_string_list_param(
                result_fields, "result_fields", allow_csv=True
            )
        except ValueError as e:
            raise_failure(create_validation_error(str(e), parameter="result_fields"))
        try:
            parsed_result_attribute_keys = parse_string_list_param(
                result_attribute_keys, "result_attribute_keys", allow_csv=True
            )
        except ValueError as e:
            raise_failure(
                create_validation_error(str(e), parameter="result_attribute_keys")
            )
        return parsed_result_fields, parsed_result_attribute_keys
