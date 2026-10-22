"""Compatibility exports for the shared form flow implementation."""

from ha_mcp.backends.legacy_flows import legacy_flow
from ha_mcp.runtime.flows import form as _shared
from ha_mcp.runtime.flows import schema as _shared_schema

ErrorCode = _shared.ErrorCode
create_error_response = _shared.create_error_response
_MENU_SELECTION_KEY_ORDER = _shared._MENU_SELECTION_KEY_ORDER
raise_tool_error = legacy_flow(_shared.raise_tool_error)
reject_malformed_list_fields = legacy_flow(_shared_schema.reject_malformed_list_fields)
_MENU_SELECTION_KEYS = _shared._MENU_SELECTION_KEYS
_PER_STEP_VALUES_KEY = _shared._PER_STEP_VALUES_KEY
_RESERVED_CONFIG_KEYS = _shared._RESERVED_CONFIG_KEYS
_MISSING_DEFAULT = _shared._MISSING_DEFAULT
_NO_SUBMISSION = _shared._NO_SUBMISSION
iter_schema_fields = legacy_flow(_shared.iter_schema_fields)
_section_path = legacy_flow(_shared._section_path)
_ReuseState = _shared._ReuseState
_record_ignored_section_keys = legacy_flow(_shared._record_ignored_section_keys)
_field_default_value = legacy_flow(_shared._field_default_value)
_schema_default_values = legacy_flow(_shared._schema_default_values)
_required_section_defaults = legacy_flow(_shared._required_section_defaults)
_ignored_keys_warnings = legacy_flow(_shared._ignored_keys_warnings)
_success_warnings = legacy_flow(_shared._success_warnings)
_consume_section_schema = legacy_flow(_shared._consume_section_schema)
_mark_consumed = legacy_flow(_shared._mark_consumed)
_auto_confirm_form_payload = legacy_flow(_shared._auto_confirm_form_payload)
_step_owned_submission_value = legacy_flow(_shared._step_owned_submission_value)
_CLEARED = _shared._CLEARED
_strip_cleared = legacy_flow(_shared._strip_cleared)
_is_redacted_value = legacy_flow(_shared._is_redacted_value)
_current_value_backfill = legacy_flow(_shared._current_value_backfill)
_clears_by_omission = legacy_flow(_shared._clears_by_omission)
_edit_mode_submission = legacy_flow(_shared._edit_mode_submission)
_redeclared_field_submission = legacy_flow(_shared._redeclared_field_submission)
_consume_leaf_field = legacy_flow(_shared._consume_leaf_field)
_consume_declared_section = legacy_flow(_shared._consume_declared_section)
_consume_form_schema = legacy_flow(_shared._consume_form_schema)
_extract_schema_field_names = legacy_flow(_shared._extract_schema_field_names)
_consume_all_remaining_keys = legacy_flow(_shared._consume_all_remaining_keys)
validate_step_values = legacy_flow(_shared.validate_step_values)
_step_values_applied = legacy_flow(_shared._step_values_applied)
_handle_form_step = legacy_flow(_shared._handle_form_step)
