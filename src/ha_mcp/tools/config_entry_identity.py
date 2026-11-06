"""Legacy facade over shared config-entry identity safeguards."""

from ha_mcp.backends.legacy_flows import legacy_helper_flow
from ha_mcp.runtime import reconfigure_identity as _shared

ReconfigureIdentity = _shared.ReconfigureIdentity
cross_domain_warnings = legacy_helper_flow(_shared.cross_domain_warnings)
_classify_related_entries = legacy_helper_flow(_shared._classify_related_entries)


__all__ = ["ReconfigureIdentity", "_classify_related_entries", "cross_domain_warnings"]
