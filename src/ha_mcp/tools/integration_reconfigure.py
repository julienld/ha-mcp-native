"""Legacy integration-reconfigure facade over the shared confirmation runner."""

from functools import partial
from typing import Any

from ha_mcp.backends.legacy import entry_write_guard
from ha_mcp.backends.legacy_flows import legacy_flow
from ha_mcp.backends.remote import RemoteBackend
from ha_mcp.runtime import integration_reconfigure as _shared
from ha_mcp.runtime.flows.session import FlowSession
from ha_mcp.tool_api import CallContext

RECONFIGURE_ONLY_PARAMETERS = _shared.RECONFIGURE_ONLY_PARAMETERS
reject_reconfigure_only_parameters = legacy_flow(
    _shared.reject_reconfigure_only_parameters
)
_reconfigure_preflight_token = _shared._reconfigure_preflight_token


class ReconfigureRunner(_shared.ReconfigureRunner):
    def __init__(self, client: Any) -> None:
        super().__init__(
            FlowSession(
                RemoteBackend(client),
                CallContext(None),
                helper_type="integration",
                capture_backup=True,
                source_tool="ha_set_integration",
                entry_guard=partial(entry_write_guard, client),
            )
        )

    run = legacy_flow(_shared.ReconfigureRunner.run)
    handle_mode = legacy_flow(_shared.ReconfigureRunner.handle_mode)


__all__ = [
    "ReconfigureRunner",
    "_reconfigure_preflight_token",
]
