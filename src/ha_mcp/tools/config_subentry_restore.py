"""Compatibility facade over the shared complete-subentry restore walker."""

from ha_mcp.backends.legacy_flows import legacy_flow
from ha_mcp.runtime.flows import subentry_restore as _shared

restore_config_subentry = legacy_flow(_shared.restore_config_subentry)
recreate_config_subentry = legacy_flow(_shared.recreate_config_subentry)
