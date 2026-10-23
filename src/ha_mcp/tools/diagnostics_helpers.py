"""Legacy diagnostics facade; projection and error semantics are shared."""

from ha_mcp.backends.legacy_flows import legacy_helper_flow
from ha_mcp.runtime import diagnostics as _shared

DIAGNOSTICS_DEFAULT_TIMEOUT_SECONDS = _shared.DIAGNOSTICS_DEFAULT_TIMEOUT_SECONDS
parse_diagnostics_fields = legacy_helper_flow(_shared.parse_diagnostics_fields)
fetch_integration_diagnostics = legacy_helper_flow(
    _shared.fetch_integration_diagnostics
)
_resolve_data_path = legacy_helper_flow(_shared._resolve_data_path)


__all__ = [
    "DIAGNOSTICS_DEFAULT_TIMEOUT_SECONDS",
    "_resolve_data_path",
    "fetch_integration_diagnostics",
    "parse_diagnostics_fields",
]
