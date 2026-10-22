"""Bind device-write metadata to direct Core services per invocation."""

from collections.abc import Awaitable, Callable
from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.device_tools import DeviceWriteTools
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import write_hints

from .native_operations.devices import NativeDevices


def build_device_tools(manager: Any) -> list[ToolDefinition]:
    definitions = []
    for name, title in (
        ("ha_set_device", "Set Device"),
        ("ha_remove_device", "Remove Device"),
    ):

        async def invoke(
            arguments: dict[str, Any],
            actor: CallContext,
            method_name: str = name,
        ) -> dict[str, Any]:
            service = NativeDevices(
                manager.hass,
                actor,
                authorize=manager.backend.authorize,
                backup=manager.snapshots.registry_backup,
            )
            method: Callable[..., Awaitable[dict[str, Any]]] = getattr(
                DeviceWriteTools(service), method_name
            )
            return await method(**arguments)

        definitions.append(
            define_tool(
                manager.backend,
                DeviceWriteTools,
                name,
                source_module="tools_registry",
                annotations=write_hints(
                    title, destructive=True, idempotent=True, open_world=False
                ),
                tags=frozenset({"Device Registry"}),
                handler=invoke,
            )
        )
    return definitions
