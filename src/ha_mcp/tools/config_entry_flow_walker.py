"""Compatibility exports for the shared walker flow implementation."""

from ha_mcp.backends.legacy_flows import legacy_flow
from ha_mcp.runtime.flows import submission as _submission
from ha_mcp.runtime.flows import walker as _shared

fetch_helper_flow_info = legacy_flow(_submission.fetch_helper_flow_info)
ReconfigureStatus = _shared.ReconfigureStatus
_parse_flow_api_error = legacy_flow(_shared._parse_flow_api_error)
_raise_flow_api_error = legacy_flow(_shared._raise_flow_api_error)
_submit_step = legacy_flow(_shared._submit_step)
_handle_flow_steps = legacy_flow(_shared._handle_flow_steps)
_handle_config_subentry_flow_steps = legacy_flow(
    _shared._handle_config_subentry_flow_steps
)


__all__ = [
    "ReconfigureStatus",
    "_handle_config_subentry_flow_steps",
    "_handle_flow_steps",
    "_parse_flow_api_error",
    "_raise_flow_api_error",
    "_submit_step",
    "fetch_helper_flow_info",
]
