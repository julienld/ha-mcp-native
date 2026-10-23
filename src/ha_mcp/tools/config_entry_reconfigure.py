"""Legacy compatibility for shared integration reconfigure orchestration."""

from ha_mcp.backends.legacy_flows import legacy_helper_flow
from ha_mcp.runtime import reconfigure as _shared
from ha_mcp.runtime import reconfigure_preflight as _preflight

logger = _shared.logger
PreparedReconfigure = _preflight.PreparedReconfigure
prepare_reconfigure_request = legacy_helper_flow(_preflight.prepare_reconfigure_request)
reconfigure_config_entry = legacy_helper_flow(_shared.reconfigure_config_entry)
