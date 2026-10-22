"""Assemble canonical Core tools with the first migration increment's catalog."""

from typing import Any

from ha_mcp.runtime.catalog import build_initial_catalog
from ha_mcp.tool_api import ToolDefinition

from .native_calendar_tools import build_calendar_tools
from .native_device_tools import build_device_tools
from .native_entity_tools import entity_removal_definition, entity_write_definition
from .native_history_tools import history_definition
from .native_label_tools import build_label_tools
from .native_log_tools import logs_definition
from .native_operations.templates import template_definition
from .native_resource_tools import build_resource_tools
from .native_trace_tools import trace_definition


def build_native_catalog(manager: Any) -> list[ToolDefinition]:
    bridge = [
        definition
        for definition in build_initial_catalog(manager.backend)
        if definition.name != "ha_config_get_label"
    ]
    return [
        *bridge,
        *build_label_tools(manager),
        *build_device_tools(manager),
        entity_removal_definition(manager),
        entity_write_definition(manager),
        *build_calendar_tools(manager),
        *build_resource_tools(manager),
        template_definition(manager.backend),
        trace_definition(manager),
        history_definition(manager),
        logs_definition(manager),
    ]
