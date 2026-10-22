"""Legacy diagnostics facade; projection and error semantics are shared."""

from ha_mcp.backends.legacy_flows import legacy_helper_flow
from ha_mcp.runtime import diagnostics as _shared

logger = _shared.logger
DIAGNOSTICS_DEFAULT_TIMEOUT_SECONDS = _shared.DIAGNOSTICS_DEFAULT_TIMEOUT_SECONDS
parse_diagnostics_fields = legacy_helper_flow(_shared.parse_diagnostics_fields)
_fetch_raw_diagnostics = legacy_helper_flow(_shared._fetch_raw_diagnostics)
fetch_integration_diagnostics = legacy_helper_flow(
    _shared.fetch_integration_diagnostics
)
_apply_data_path_resolution = legacy_helper_flow(_shared._apply_data_path_resolution)
_apply_truncation_cap = legacy_helper_flow(_shared._apply_truncation_cap)
_project_cap_and_paginate_diagnostics = legacy_helper_flow(
    _shared._project_cap_and_paginate_diagnostics
)
_resolve_data_path = legacy_helper_flow(_shared._resolve_data_path)
