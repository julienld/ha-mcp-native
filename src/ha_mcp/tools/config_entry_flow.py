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
answer_menu_from_snapshot = legacy_flow(_shared.answer_menu_from_snapshot)
POST_COMMIT_STATUSES = _shared.POST_COMMIT_STATUSES
raise_tool_error = legacy_flow(_shared.raise_tool_error)
logger = _shared.logger
OptionsRestoreReason = _shared.OptionsRestoreReason
OptionsFlowError = _shared.OptionsFlowError
CreationFlowError = _shared.CreationFlowError
_reject_redaction_sentinels = legacy_flow(_shared._reject_redaction_sentinels)
set_config_subentry = legacy_flow(_shared.set_config_subentry)
get_user_step_field_names = legacy_flow(_shared.get_user_step_field_names)
update_config_entry_options = legacy_flow(_shared.update_config_entry_options)
config_entry_of_domain = legacy_flow(_shared.config_entry_of_domain)
COMPONENT_DOMAIN = _shared.COMPONENT_DOMAIN
COMPONENT_ENTRY_SUGGESTIONS = _shared.COMPONENT_ENTRY_SUGGESTIONS
is_component_domain = legacy_flow(_shared.is_component_domain)
refuse_component_entry = legacy_flow(_shared.refuse_component_entry)
update_flow_helper = legacy_flow(_shared.update_flow_helper)
create_config_entry = legacy_flow(_shared.create_config_entry)
create_flow_helper = legacy_flow(_shared.create_flow_helper)


__all__ = [
    "_reject_redaction_sentinels",
]
