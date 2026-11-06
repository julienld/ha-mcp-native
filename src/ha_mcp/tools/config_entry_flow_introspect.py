"""Compatibility exports for the shared introspect flow implementation."""

from ha_mcp.backends.legacy_flows import legacy_flow
from ha_mcp.runtime.flows import introspect as _shared

HomeAssistantAPIError = _shared.HomeAssistantAPIError
logger = _shared.logger
menu_choices = legacy_flow(_shared.menu_choices)
fetch_helper_flow_info = legacy_flow(_shared.fetch_helper_flow_info)
