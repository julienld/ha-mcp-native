"""Compatibility exports for the shared walker flow implementation."""

from ha_mcp.backends.legacy_flows import legacy_flow
from ha_mcp.runtime.flows import submission as _submission
from ha_mcp.runtime.flows import walker as _shared

HomeAssistantAPIError = _submission.HomeAssistantAPIError
ErrorCode = _shared.ErrorCode
create_error_response = _shared.create_error_response
redact_flow_schema = _submission.redact_flow_schema
redaction_enabled = _submission.redaction_enabled
validate_step_values = legacy_flow(_shared.validate_step_values)
fetch_helper_flow_info = legacy_flow(_submission.fetch_helper_flow_info)
raise_tool_error = legacy_flow(_shared.raise_tool_error)
logger = _shared.logger
ReconfigureStatus = _shared.ReconfigureStatus
POST_COMMIT_STATUSES = _shared.POST_COMMIT_STATUSES
_parse_flow_api_error = legacy_flow(_shared._parse_flow_api_error)
_raise_flow_api_error = legacy_flow(_shared._raise_flow_api_error)
_submit_step = legacy_flow(_shared._submit_step)
_handle_flow_steps = legacy_flow(_shared._handle_flow_steps)
_handle_config_subentry_flow_steps = legacy_flow(
    _shared._handle_config_subentry_flow_steps
)


__all__ = [
    "_handle_config_subentry_flow_steps",
    "_handle_flow_steps",
    "_parse_flow_api_error",
    "_raise_flow_api_error",
    "_submit_step",
]
