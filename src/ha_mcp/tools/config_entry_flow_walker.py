"""Compatibility exports for the shared walker flow implementation."""

from ha_mcp.backends.legacy_flows import legacy_flow
from ha_mcp.runtime.flows import submission as _submission
from ha_mcp.runtime.flows import walker as _shared

HomeAssistantAPIError = _submission.HomeAssistantAPIError
ErrorCode = _shared.ErrorCode
create_error_response = _shared.create_error_response
redact_flow_schema = _submission.redact_flow_schema
redaction_enabled = _submission.redaction_enabled
_MENU_SELECTION_KEYS = _shared._MENU_SELECTION_KEYS
_PER_STEP_VALUES_KEY = _shared._PER_STEP_VALUES_KEY
_auto_confirm_form_payload = legacy_flow(_shared._auto_confirm_form_payload)
_handle_form_step = legacy_flow(_shared._handle_form_step)
_ReuseState = _shared._ReuseState
_success_warnings = legacy_flow(_shared._success_warnings)
validate_step_values = legacy_flow(_shared.validate_step_values)
_FlowType = _shared._FlowType
fetch_helper_flow_info = legacy_flow(_submission.fetch_helper_flow_info)
_flow_step_budget = legacy_flow(_shared._flow_step_budget)
_handle_menu_step = legacy_flow(_shared._handle_menu_step)
raise_tool_error = legacy_flow(_shared.raise_tool_error)
logger = _shared.logger
_UNDRIVABLE_STEP_TYPES = _shared._UNDRIVABLE_STEP_TYPES
_RECONFIGURE_SUCCESS_REASONS = _shared._RECONFIGURE_SUCCESS_REASONS
ReconfigureStatus = _shared.ReconfigureStatus
POST_COMMIT_STATUSES = _shared.POST_COMMIT_STATUSES
_parse_flow_api_error = legacy_flow(_shared._parse_flow_api_error)
_build_flow_error_context = legacy_flow(_shared._build_flow_error_context)
_raise_flow_api_error = legacy_flow(_shared._raise_flow_api_error)
_submit_step = legacy_flow(_shared._submit_step)
_raise_reconfigure_no_answer = legacy_flow(_shared._raise_reconfigure_no_answer)
_empty_forms_suggestions = legacy_flow(_shared._empty_forms_suggestions)
_finish_flow_entry = legacy_flow(_shared._finish_flow_entry)
_flow_failure_context = legacy_flow(_shared._flow_failure_context)
_raise_flow_abort = legacy_flow(_shared._raise_flow_abort)
_unconsumed_reconfigure_keys = legacy_flow(_shared._unconsumed_reconfigure_keys)
_reconfigure_success_response = legacy_flow(_shared._reconfigure_success_response)
_reconfigure_abort_result = legacy_flow(_shared._reconfigure_abort_result)
_handle_abort_step = legacy_flow(_shared._handle_abort_step)
_flow_form_payload = legacy_flow(_shared._flow_form_payload)
_handle_flow_create_entry = legacy_flow(_shared._handle_flow_create_entry)
_handle_flow_steps = legacy_flow(_shared._handle_flow_steps)
_handle_subentry_create_entry = legacy_flow(_shared._handle_subentry_create_entry)
_handle_subentry_abort = legacy_flow(_shared._handle_subentry_abort)
_handle_config_subentry_flow_steps = legacy_flow(
    _shared._handle_config_subentry_flow_steps
)
