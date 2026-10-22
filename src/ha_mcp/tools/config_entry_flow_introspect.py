"""Compatibility exports for the shared introspect flow implementation."""

from ha_mcp.backends.legacy_flows import legacy_flow
from ha_mcp.runtime.flows import introspect as _shared

HomeAssistantAPIError = _shared.HomeAssistantAPIError
logger = _shared.logger
_FlowType = _shared._FlowType
_form_info = legacy_flow(_shared._form_info)
_process_menu_flow_result = legacy_flow(_shared._process_menu_flow_result)
menu_choices = legacy_flow(_shared.menu_choices)
fetch_helper_flow_info = legacy_flow(_shared.fetch_helper_flow_info)
