"""Legacy projections over the shared helper-list record shapes."""

from ha_mcp.backends.legacy import legacy_errors, legacy_errors_sync
from ha_mcp.runtime import helper_listing_shape as _shared

logger = _shared.logger
_SCRUB_DEGRADED_WARNING = _shared._SCRUB_DEGRADED_WARNING
_component_warnings = _shared._component_warnings
raise_if_helper_flows_degraded = legacy_errors_sync(
    _shared.raise_if_helper_flows_degraded
)
listed_items = _shared.listed_items
_shape_collection_helper_record = _shared._shape_collection_helper_record
_shape_flow_helper_record = _shared._shape_flow_helper_record
_paginate_helpers_response = _shared._paginate_helpers_response
_shape_component_helpers_response = _shared._shape_component_helpers_response
_component_covers = _shared._component_covers
_raise_flow_requires_component = legacy_errors_sync(
    _shared._raise_flow_requires_component
)
_raise_all_requires_component = legacy_errors_sync(
    _shared._raise_all_requires_component
)
shape_all_helpers_response = legacy_errors(_shared.shape_all_helpers_response)
