"""Bind Assist's shared tool to the installed pipeline and conversation owners."""

from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.pipeline_tools import PipelineTools
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import write_hints

from .native_operations.pipeline_store import NativePipelines
from .native_operations.registry_access import registry_failure


def pipeline_definition(manager: Any) -> ToolDefinition:
    async def manage(arguments: dict[str, Any], actor: CallContext) -> dict[str, Any]:
        service = NativePipelines(
            manager.hass, actor, authorize=manager.backend.authorize
        )
        try:
            return await PipelineTools(service).ha_manage_pipeline(**arguments)
        except Exception as error:
            raise registry_failure(error, {"operation": "assist_pipeline"}) from error

    return define_tool(
        manager.backend,
        PipelineTools,
        "ha_manage_pipeline",
        handler=manage,
        source_module="tools_assist_pipeline",
        tags=frozenset({"Assist"}),
        annotations=write_hints(
            "Manage Assist Pipeline",
            destructive=True,
            idempotent=False,
            open_world=True,
        ),
        writes=lambda arguments: arguments["action"] not in {"list", "get"},
    )
