"""Legacy helper-flow exports using the shared orchestration."""

from ha_mcp.backends.legacy_flows import legacy_helper_flow
from ha_mcp.runtime.flows import helpers as _shared

logger = _shared.logger
_MENU_CHOICE_CONFIG_KEYS = _shared._MENU_CHOICE_CONFIG_KEYS
_extract_menu_choice_from_config = legacy_helper_flow(_shared._extract_menu_choice_from_config)
_flow_helper_error_context = legacy_helper_flow(_shared._flow_helper_error_context)
_resolve_flow_action = legacy_helper_flow(_shared._resolve_flow_action)
_normalize_flow_config = legacy_helper_flow(_shared._normalize_flow_config)
_inject_or_strip_name = legacy_helper_flow(_shared._inject_or_strip_name)
_wait_for_flow_entities = legacy_helper_flow(_shared._wait_for_flow_entities)
_apply_flow_registry_updates = legacy_helper_flow(_shared._apply_flow_registry_updates)
_handle_flow_helper = legacy_helper_flow(_shared._handle_flow_helper)
_handle_set_config_subentry = legacy_helper_flow(_shared._handle_set_config_subentry)
