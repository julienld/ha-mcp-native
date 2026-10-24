"""Bind the two public YAML contracts to local component owners."""

from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.yaml_read_tools import YamlReadTools
from ha_mcp.runtime.yaml_tools import YamlConfigTools
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import read_only_hints, write_hints

from .native_operations.yaml_config import NativeYamlConfig, NativeYamlRead


def yaml_definitions(manager: Any) -> list[ToolDefinition]:
    async def read(arguments: dict[str, Any], actor: CallContext) -> dict[str, Any]:
        return await YamlReadTools(NativeYamlRead(manager, actor)).ha_config_get_yaml(
            **arguments
        )

    async def edit(arguments: dict[str, Any], actor: CallContext) -> dict[str, Any]:
        return await YamlConfigTools(
            NativeYamlConfig(manager, actor)
        ).ha_config_set_yaml(**arguments)

    return [
        define_tool(
            manager.backend,
            YamlReadTools,
            "ha_config_get_yaml",
            handler=read,
            source_module="tools_yaml_read",
            tags=frozenset({"System", "beta"}),
            annotations=read_only_hints("Read YAML Config Fragment", open_world=False),
        ),
        define_tool(
            manager.backend,
            YamlConfigTools,
            "ha_config_set_yaml",
            handler=edit,
            source_module="tools_yaml_config",
            tags=frozenset({"System", "beta"}),
            annotations=write_hints(
                "Raw YAML Config Edit",
                destructive=True,
                idempotent=False,
                open_world=False,
            ),
        ),
    ]
