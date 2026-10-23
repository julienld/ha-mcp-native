"""Legacy facade for shared helper-field descriptions."""

from ha_mcp.backends.legacy_flows import legacy_helper_flow
from ha_mcp.runtime import helper_describe as _shared

logger = _shared.logger
MAX_OPTIONS = _shared.MAX_OPTIONS
compact_field = legacy_helper_flow(_shared.compact_field)
compact_schema = legacy_helper_flow(_shared.compact_schema)
describe_helper = legacy_helper_flow(_shared.describe_helper)
describe_helper_response = legacy_helper_flow(_shared.describe_helper_response)
