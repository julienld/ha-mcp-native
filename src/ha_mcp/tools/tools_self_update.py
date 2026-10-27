"""Auto-discovered external/legacy projection of the self-update contract."""

from typing import Any

from ha_mcp._vendor.fastmcp.tools import tool
from ha_mcp.backends.legacy import legacy_errors
from ha_mcp.runtime.self_update_tools import SelfUpdateFacade
from ha_mcp.self_update import ProcessSelfUpdate

from .helpers import log_tool_usage, register_tool_methods
from .tool_hints import write_hints


class SelfUpdateTools(SelfUpdateFacade):
    def __init__(self) -> None:
        super().__init__(ProcessSelfUpdate().manage)

    ha_manage_ha_mcp_updates = tool(
        name="ha_manage_ha_mcp_updates",
        tags={"System"},
        annotations=write_hints(
            "Update HA-MCP", destructive=True, idempotent=False, open_world=True
        ),
    )(log_tool_usage(legacy_errors(SelfUpdateFacade.ha_manage_ha_mcp_updates)))


def register_self_update_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    register_tool_methods(mcp, SelfUpdateTools())
