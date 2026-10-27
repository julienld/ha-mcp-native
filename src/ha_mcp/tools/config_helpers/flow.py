"""Legacy helper-flow exports using the shared orchestration."""

from ha_mcp.backends.legacy_flows import legacy_helper_flow
from ha_mcp.runtime.flows import helpers as _shared

_extract_menu_choice_from_config = legacy_helper_flow(
    _shared._extract_menu_choice_from_config
)
_flow_helper_error_context = legacy_helper_flow(_shared._flow_helper_error_context)
_wait_for_flow_entities = legacy_helper_flow(_shared._wait_for_flow_entities)
_handle_flow_helper = legacy_helper_flow(_shared._handle_flow_helper)
_handle_set_config_subentry = legacy_helper_flow(_shared._handle_set_config_subentry)


__all__ = [
    "_extract_menu_choice_from_config",
    "_flow_helper_error_context",
    "_handle_flow_helper",
    "_handle_set_config_subentry",
    "_wait_for_flow_entities",
]
