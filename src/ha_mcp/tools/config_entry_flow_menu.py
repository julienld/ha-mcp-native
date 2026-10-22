"""Compatibility exports for the shared menu flow implementation."""

from ha_mcp.backends.legacy_flows import legacy_flow
from ha_mcp.runtime.flows import menu as _shared

ErrorCode = _shared.ErrorCode
create_error_response = _shared.create_error_response
raise_tool_error = legacy_flow(_shared.raise_tool_error)
_MENU_SELECTION_KEY_ORDER = _shared._MENU_SELECTION_KEY_ORDER
_handle_menu_step = legacy_flow(_shared._handle_menu_step)
_raise_missing_menu_selection = legacy_flow(_shared._raise_missing_menu_selection)
_flow_step_budget = legacy_flow(_shared._flow_step_budget)
_MENU_BRANCH_KEYS = _shared._MENU_BRANCH_KEYS
answer_menu_from_snapshot = legacy_flow(_shared.answer_menu_from_snapshot)
