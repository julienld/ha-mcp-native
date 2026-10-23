"""Compatibility exports for the shared form flow implementation."""

from ha_mcp.backends.legacy_flows import legacy_flow
from ha_mcp.runtime.flows import form as _shared
from ha_mcp.runtime.flows import schema as _shared_schema

ErrorCode = _shared.ErrorCode
create_error_response = _shared.create_error_response
raise_tool_error = legacy_flow(_shared.raise_tool_error)
reject_malformed_list_fields = legacy_flow(_shared_schema.reject_malformed_list_fields)
_MISSING_DEFAULT = _shared._MISSING_DEFAULT
iter_schema_fields = legacy_flow(_shared.iter_schema_fields)
_ReuseState = _shared._ReuseState
_ignored_keys_warnings = legacy_flow(_shared._ignored_keys_warnings)
_step_owned_submission_value = legacy_flow(_shared._step_owned_submission_value)
_consume_form_schema = legacy_flow(_shared._consume_form_schema)
_extract_schema_field_names = legacy_flow(_shared._extract_schema_field_names)
validate_step_values = legacy_flow(_shared.validate_step_values)
_handle_form_step = legacy_flow(_shared._handle_form_step)


__all__ = [
    "_MISSING_DEFAULT",
    "_ReuseState",
    "_consume_form_schema",
    "_extract_schema_field_names",
    "_handle_form_step",
    "_ignored_keys_warnings",
    "_step_owned_submission_value",
]
