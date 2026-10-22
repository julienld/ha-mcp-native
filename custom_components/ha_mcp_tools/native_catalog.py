"""Assemble canonical Core tools with the first migration increment's catalog."""

from typing import Any

from ha_mcp.runtime.catalog import build_initial_catalog
from ha_mcp.tool_api import ToolDefinition

from .native_blueprint_tools import blueprint_definition
from .native_bulk_tools import bulk_definitions
from .native_calendar_tools import build_calendar_tools
from .native_camera_tools import camera_definition
from .native_dashboard_tools import build_dashboard_tools
from .native_device_tools import build_device_tools
from .native_energy_tools import energy_definition
from .native_entity_tools import entity_removal_definition, entity_write_definition
from .native_event_tools import event_definition
from .native_group_tools import build_group_tools
from .native_health_tools import health_definition
from .native_history_tools import history_definition
from .native_label_tools import build_label_tools
from .native_log_tools import logs_definition
from .native_operations.templates import template_definition
from .native_overview_tools import overview_definition
from .native_pipeline_tools import pipeline_definition
from .native_radio_tools import radio_definition
from .native_resource_tools import build_resource_tools
from .native_search_tools import search_definition
from .native_system_tools import system_definitions
from .native_theme_tools import theme_definition
from .native_trace_tools import trace_definition
from .native_update_tools import update_definition


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
        *bulk_definitions(manager),
        entity_removal_definition(manager),
        entity_write_definition(manager),
        energy_definition(manager),
        event_definition(manager),
        *build_group_tools(manager),
        camera_definition(manager),
        overview_definition(manager),
        pipeline_definition(manager),
        radio_definition(manager),
        search_definition(manager),
        health_definition(manager),
        *system_definitions(manager),
        theme_definition(manager),
        update_definition(manager),
        *build_calendar_tools(manager),
        *build_resource_tools(manager),
        *build_dashboard_tools(manager),
        blueprint_definition(manager),
        template_definition(manager.backend),
        trace_definition(manager),
        history_definition(manager),
        logs_definition(manager),
    ]
