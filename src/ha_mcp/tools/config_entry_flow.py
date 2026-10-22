"""Compatibility exports for the shared entry flow implementation."""

from ha_mcp.backends.legacy_flows import legacy_flow
from ha_mcp.runtime.flows import entry as _shared

defaultdict = _shared.defaultdict
HomeAssistantAPIError = _shared.HomeAssistantAPIError
HomeAssistantAuthError = _shared.HomeAssistantAuthError
HomeAssistantError = _shared.HomeAssistantError
ErrorCode = _shared.ErrorCode
create_error_response = _shared.create_error_response
sentinel_option_keys = _shared.sentinel_option_keys
_extract_schema_field_names = legacy_flow(_shared._extract_schema_field_names)
answer_menu_from_snapshot = legacy_flow(_shared.answer_menu_from_snapshot)
POST_COMMIT_STATUSES = _shared.POST_COMMIT_STATUSES
_FlowType = _shared._FlowType
_handle_config_subentry_flow_steps = legacy_flow(
    _shared._handle_config_subentry_flow_steps
)
_handle_flow_steps = legacy_flow(_shared._handle_flow_steps)
raise_tool_error = legacy_flow(_shared.raise_tool_error)
logger = _shared.logger
OptionsRestoreReason = _shared.OptionsRestoreReason
_RESTORE_REASON_MESSAGES = _shared._RESTORE_REASON_MESSAGES
_SAFE_RESTORE_FIELD_NAMES = _shared._SAFE_RESTORE_FIELD_NAMES
OptionsFlowError = _shared.OptionsFlowError
CreationFlowError = _shared.CreationFlowError
_OptionsFlowProgress = _shared._OptionsFlowProgress
_json_differs = legacy_flow(_shared._json_differs)
_unknown_snapshot_fields = legacy_flow(_shared._unknown_snapshot_fields)
_CreationFlowProgress = _shared._CreationFlowProgress
_create_snapshot_helper = legacy_flow(_shared._create_snapshot_helper)
_cleanup_snapshot_creation = legacy_flow(_shared._cleanup_snapshot_creation)
_abort_flow_best_effort = legacy_flow(_shared._abort_flow_best_effort)
_abort_subentry_flow_best_effort = legacy_flow(_shared._abort_subentry_flow_best_effort)
_reject_redaction_sentinels = legacy_flow(_shared._reject_redaction_sentinels)
_subentry_ids = legacy_flow(_shared._subentry_ids)
_created_subentry_id = legacy_flow(_shared._created_subentry_id)
_SUBENTRY_CREATE_LOCKS = _shared._SUBENTRY_CREATE_LOCKS
set_config_subentry = legacy_flow(_shared.set_config_subentry)
_run_config_subentry_flow = legacy_flow(_shared._run_config_subentry_flow)
get_user_step_field_names = legacy_flow(_shared.get_user_step_field_names)
update_config_entry_options = legacy_flow(_shared.update_config_entry_options)
config_entry_of_domain = legacy_flow(_shared.config_entry_of_domain)
COMPONENT_DOMAIN = _shared.COMPONENT_DOMAIN
COMPONENT_ENTRY_SUGGESTIONS = _shared.COMPONENT_ENTRY_SUGGESTIONS
is_component_domain = legacy_flow(_shared.is_component_domain)
refuse_component_entry = legacy_flow(_shared.refuse_component_entry)
_update_config_entry_options = legacy_flow(_shared._update_config_entry_options)
update_flow_helper = legacy_flow(_shared.update_flow_helper)
create_config_entry = legacy_flow(_shared.create_config_entry)
create_flow_helper = legacy_flow(_shared.create_flow_helper)
