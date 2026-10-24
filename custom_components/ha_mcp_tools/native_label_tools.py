"""Bind label tool metadata to its canonical Core service per invocation."""

from collections.abc import Awaitable, Callable
from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.label_tools import LabelTools
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import read_only_hints, write_hints

from .native_operations.labels import NativeLabels


def build_label_tools(manager: Any) -> list[ToolDefinition]:
    definitions = []
    for name, title, readonly, idempotent in (
        ("ha_config_get_label", "Get Label", True, True),
        ("ha_config_set_label", "Create or Update Label", False, False),
        ("ha_config_remove_label", "Remove Label", False, True),
    ):

        async def invoke(
            arguments: dict[str, Any], actor: CallContext, method_name: str = name
        ) -> dict[str, Any]:
            tools = LabelTools(
                NativeLabels(
                    manager.hass,
                    actor,
                    authorize=manager.backend.authorize,
                    backup=manager.snapshots.registry_backup,
                )
            )
            method: Callable[..., Awaitable[dict[str, Any]]] = getattr(
                tools, method_name
            )
            return await method(**arguments)

        definitions.append(
            define_tool(
                manager.backend,
                LabelTools,
                name,
                source_module="tools_labels",
                annotations=read_only_hints(title, open_world=False)
                if readonly
                else write_hints(
                    title, destructive=True, idempotent=idempotent, open_world=False
                ),
                tags=frozenset({"Labels & Categories"}),
                handler=invoke,
            )
        )
    return definitions
