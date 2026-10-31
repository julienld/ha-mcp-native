"""Compatibility exports for the shared menu flow implementation."""

from ha_mcp.backends.legacy_flows import legacy_flow
from ha_mcp.runtime.flows import menu as _shared

ErrorCode = _shared.ErrorCode
create_error_response = _shared.create_error_response
raise_tool_error = legacy_flow(_shared.raise_tool_error)
answer_menu_from_snapshot = legacy_flow(_shared.answer_menu_from_snapshot)
