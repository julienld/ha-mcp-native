"""Legacy error projection for the shared dashboard list-shape checks."""

from ha_mcp.backends.legacy import legacy_errors_sync
from ha_mcp.runtime import dashboard_validation as _shared

reject_malformed_dashboard_lists = legacy_errors_sync(
    _shared.reject_malformed_dashboard_lists
)
reject_malformed_dashboard_config = legacy_errors_sync(
    _shared.reject_malformed_dashboard_config
)
patch_writes_inside_card = _shared.patch_writes_inside_card
reject_malformed_dashboard_patch = legacy_errors_sync(
    _shared.reject_malformed_dashboard_patch
)
