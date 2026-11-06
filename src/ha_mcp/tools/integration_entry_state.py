"""Legacy facade for shared integration enable and logger modes."""

from ha_mcp.backends.legacy_flows import legacy_helper_flow
from ha_mcp.runtime import integration_state as _shared

set_log_level = legacy_helper_flow(_shared.set_log_level)
set_entry_enabled = legacy_helper_flow(_shared.set_entry_enabled)
