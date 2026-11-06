"""The HA instance and live actor validator shared by native mutation owners."""

from collections.abc import Awaitable, Callable
from typing import Any

from ha_mcp.tool_api import CallContext


class CoreOperationOwner:
    def __init__(
        self, hass: Any, authorize: Callable[[CallContext], Awaitable[None]]
    ) -> None:
        self.hass = hass
        self.authorize = authorize
