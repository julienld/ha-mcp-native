"""Bind the one public dashboard contract to the native Core service."""

from collections.abc import Awaitable, Callable
from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.dashboard_tools import DashboardTools
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import write_hints

from .native_operations.dashboard_advice import dashboard_read_writes
from .native_operations.lovelace_dashboards import NativeDashboards


def build_dashboard_tools(manager: Any) -> list[ToolDefinition]:
    definitions = []
    for name, title, idempotent in (
        ("ha_config_get_dashboard", "Get Dashboard", False),
        ("ha_config_set_dashboard", "Create or Update Dashboard", False),
        ("ha_config_delete_dashboard", "Delete Dashboard", True),
    ):

        async def invoke(
            arguments: dict[str, Any], actor: CallContext, method_name: str = name
        ) -> Any:
            method: Callable[..., Awaitable[Any]] = getattr(
                DashboardTools(NativeDashboards(manager, actor)), method_name
            )
            return await method(**arguments)

        reading = name == "ha_config_get_dashboard"
        definitions.append(
            define_tool(
                manager.backend,
                DashboardTools,
                name,
                handler=invoke,
                source_module="tools_config_dashboards",
                tags=frozenset({"Dashboards"}),
                annotations=write_hints(
                    title,
                    destructive=True,
                    idempotent=idempotent,
                    open_world=not name.endswith("delete_dashboard"),
                ),
                writes=(
                    lambda arguments: dashboard_read_writes(manager.hass, arguments)
                )
                if reading
                else None,
                meta={
                    "ha_mcp": {
                        "native_support": {
                            "note": "Dashboard config/list/search reads do not write. Describing custom cards or listing card types can initialize Core resources and migrate legacy resources; that first inspection requires write admission. include_screenshot also requires write admission because the renderer may persist its user's profile."
                        }
                    }
                }
                if reading
                else {},
            )
        )
    return definitions
