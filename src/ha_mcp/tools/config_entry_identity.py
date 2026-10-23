"""Legacy facade over shared config-entry identity safeguards."""

from ha_mcp.backends.legacy_flows import legacy_helper_flow
from ha_mcp.runtime import reconfigure_identity as _shared

logger = _shared.logger
ReconfigureIdentity = _shared.ReconfigureIdentity
RelatedEntries = _shared.RelatedEntries
cross_domain_warnings = legacy_helper_flow(_shared.cross_domain_warnings)
_classify_related_entries = legacy_helper_flow(_shared._classify_related_entries)

build_reconfigure_rollback_metadata = legacy_helper_flow(
    _shared.build_reconfigure_rollback_metadata
)


__all__ = [
    "_classify_related_entries",
]
