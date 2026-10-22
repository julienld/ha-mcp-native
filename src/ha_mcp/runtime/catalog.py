"""Project shared Python tool signatures into the neutral catalog."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import replace
from typing import Any, get_type_hints

from pydantic import ConfigDict, TypeAdapter, ValidationError, create_model

from ha_mcp.tool_api import (
    Backend,
    CallContext,
    ToolDefinition,
    ToolFailure,
    ToolHandler,
)
from ha_mcp.tool_api.parameters import hidden_param_names
from ha_mcp.tools.tool_hints import read_only_hints, write_hints

from .actions import ServiceCallTools
from .area_writes import AreaWriteTools
from .automation_writes import AutomationWriteTools
from .category_writes import CategoryWriteTools
from .config_reads import AutomationReadTools, SceneReadTools, ScriptReadTools
from .config_removals import (
    AutomationRemovalTools,
    SceneRemovalTools,
    ScriptRemovalTools,
)
from .devices import DeviceDiscoveryTools
from .entities import EntityDiscoveryTools
from .exposure import ExposureTools
from .helper_listing import HelperListingTools, description_starts_flow
from .helper_schema import SIMPLE_HELPER_TYPES
from .helper_tools import HelperConfigTools
from .integration_reads import IntegrationReadTools, inspection_starts_flow
from .integration_removals import IntegrationRemovalTools
from .integration_writes import IntegrationWriteTools, integration_changes_state
from .labels import CategoryDiscoveryTools, LabelDiscoveryTools
from .registries import AreaDiscoveryTools
from .scene_writes import SceneWriteTools
from .script_writes import ScriptWriteTools
from .services import ServiceDiscoveryTools
from .skills import SkillGuideTools
from .states import StateTools
from .todo import TodoTools
from .tool_effects import READ_ONLY_EXEMPT_TOOLS
from .zone_writes import ZoneWriteTools
from .zones import ZoneReadTools


def define_tool(
    backend: Backend,
    owner: type[Any],
    method_name: str,
    *,
    annotations: dict[str, Any],
    tags: frozenset[str] = frozenset(),
    source_module: str | None = None,
    meta: dict[str, Any] | None = None,
    writes: Callable[[dict[str, Any]], bool] | None = None,
    handler: ToolHandler | None = None,
) -> ToolDefinition:
    """One signature drives validation, schemas and both transport facades."""
    method = getattr(owner, method_name)
    exemption = READ_ONLY_EXEMPT_TOOLS.get(method_name)
    if writes is None and exemption is not None:

        def writes(arguments: dict[str, Any]) -> bool:
            assert exemption is not None
            return exemption.blocked_write(arguments) is not None

    signature = inspect.signature(method)
    hints = get_type_hints(method, include_extras=True)
    fields: dict[str, Any] = {
        name: (
            hints.get(name, Any),
            ... if parameter.default is inspect.Parameter.empty else parameter.default,
        )
        for name, parameter in signature.parameters.items()
        if name != "self"
    }
    model = create_model(method_name, __config__=ConfigDict(extra="forbid"), **fields)

    def validate(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            validated = model.model_validate(arguments)
        except ValidationError as exc:
            raise ToolFailure("INVALID_ARGUMENTS", str(exc)) from exc
        # model_dump recursively changes arbitrary user objects. Tool parameters
        # should retain the values the authoritative validator produced.
        return {name: getattr(validated, name) for name in model.model_fields}

    async def invoke(arguments: dict[str, Any], context: CallContext) -> Any:
        bound: Callable[..., Any] = getattr(owner(backend, context), method_name)
        return await bound(**arguments)

    input_schema = _without_schema_titles(model.model_json_schema())
    for name in hidden_param_names(method):
        input_schema["properties"].pop(name, None)
        if name in input_schema.get("required", []):
            input_schema["required"].remove(name)
    return ToolDefinition(
        method_name,
        inspect.getdoc(method) or "",
        input_schema,
        handler or invoke,
        annotations,
        output_schema=_without_schema_titles(
            TypeAdapter(hints.get("return", Any)).json_schema()
        ),
        validate=validate,
        tags=tags,
        source_module=source_module,
        meta=meta or {},
        writes=writes,
    )


def _without_schema_titles(schema: Any) -> Any:
    """Match existing MCP schema presentation without touching literal defaults."""
    if not isinstance(schema, dict):
        return schema
    result = {key: value for key, value in schema.items() if key != "title"}
    for key in ("properties", "$defs", "patternProperties"):
        if isinstance(result.get(key), dict):
            result[key] = {
                name: _without_schema_titles(value)
                for name, value in result[key].items()
            }
    for key in ("items", "additionalProperties", "not"):
        if isinstance(result.get(key), dict):
            result[key] = _without_schema_titles(result[key])
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        if isinstance(result.get(key), list):
            result[key] = [_without_schema_titles(item) for item in result[key]]
    return result


def _integration_reader(backend: Backend) -> ToolDefinition:
    definition = define_tool(
        backend,
        IntegrationReadTools,
        "ha_get_integration",
        source_module="tools_integrations",
        annotations=write_hints(
            "Get Integration", destructive=True, idempotent=False, open_world=False
        ),
        tags=frozenset({"Integrations"}),
        writes=lambda arguments: inspection_starts_flow(
            arguments, redact=getattr(backend, "redaction_enabled", None)
        ),
    )
    properties = definition.input_schema["properties"]
    properties["include_options"]["description"] = (
        "Include persisted options for each entry; automatically enabled when domain is set. "
        "With secret redaction enabled, reading options can start an integration's options flow to inspect password fields."
    )
    properties["include_schema"]["description"] += (
        " Opens the actual Core options flow, which may change configuration immediately."
    )
    properties["include_subentry_schema"]["description"] = (
        "When entry_id is set, open the actual config subentry flow and return its first step. "
        "A flow may create or change a subentry immediately. Pair with subentry_type and optionally "
        "subentry_id for reconfiguration. Pending forms are closed after inspection."
    )
    return replace(
        definition,
        description=definition.description
        + (
            "\n\nNATIVE FLOW EFFECTS: options/subentry schema inspection and password-field redaction "
            "can initialize Core flows with immediate effects. These branches require write admission; "
            "ordinary persisted-value reads do not open flows when secret redaction is disabled."
        ),
    )


def build_initial_catalog(backend: Backend) -> list[ToolDefinition]:
    """Only complete migrated tools are advertised by the native entry."""
    return [
        *(
            define_tool(
                backend,
                TodoTools,
                name,
                source_module="tools_todo",
                annotations=annotations,
                tags=frozenset({"Todo Lists"}),
            )
            for name, annotations in (
                ("ha_get_todo", read_only_hints("Get Todo", open_world=False)),
                (
                    "ha_set_todo_item",
                    write_hints(
                        "Set Todo Item",
                        destructive=True,
                        idempotent=False,
                        open_world=False,
                    ),
                ),
                (
                    "ha_remove_todo_item",
                    write_hints(
                        "Remove Todo Item",
                        destructive=True,
                        idempotent=True,
                        open_world=False,
                    ),
                ),
            )
        ),
        _integration_reader(backend),
        define_tool(
            backend,
            IntegrationWriteTools,
            "ha_set_integration",
            source_module="tools_integrations",
            annotations=write_hints(
                "Set Integration", destructive=True, idempotent=False, open_world=False
            ),
            tags=frozenset({"Integrations"}),
            writes=integration_changes_state,
        ),
        define_tool(
            backend,
            IntegrationRemovalTools,
            "ha_remove_helpers_integrations",
            source_module="tools_integrations",
            annotations=write_hints(
                "Remove Helper or Integration",
                destructive=True,
                idempotent=True,
                open_world=False,
            ),
            tags=frozenset({"Helper Entities", "Integrations"}),
        ),
        *(
            define_tool(
                backend,
                owner,
                name,
                source_module=module,
                annotations=write_hints(
                    title, destructive=True, idempotent=deletes, open_world=False
                ),
                tags=frozenset({tag}),
            )
            for owner, name, module, title, tag, deletes in (
                (
                    CategoryWriteTools,
                    "ha_config_set_category",
                    "tools_categories",
                    "Create or Update Category",
                    "Labels & Categories",
                    False,
                ),
                (
                    CategoryWriteTools,
                    "ha_config_remove_category",
                    "tools_categories",
                    "Remove Category",
                    "Labels & Categories",
                    True,
                ),
                (
                    AreaWriteTools,
                    "ha_set_area_or_floor",
                    "tools_areas",
                    "Create or Update Area or Floor",
                    "Areas & Floors",
                    False,
                ),
                (
                    AreaWriteTools,
                    "ha_remove_area_or_floor",
                    "tools_areas",
                    "Remove Area or Floor",
                    "Areas & Floors",
                    True,
                ),
            )
        ),
        define_tool(
            backend,
            AutomationWriteTools,
            "ha_config_set_automation",
            source_module="tools_config_automations",
            annotations=write_hints(
                "Create or Update Automation",
                destructive=True,
                idempotent=False,
                open_world=False,
            ),
            tags=frozenset({"Automations"}),
        ),
        define_tool(
            backend,
            SceneWriteTools,
            "ha_config_set_scene",
            source_module="tools_config_scenes",
            annotations=write_hints(
                "Create or Update Scene",
                destructive=True,
                idempotent=False,
                open_world=False,
            ),
            tags=frozenset({"Scenes"}),
        ),
        define_tool(
            backend,
            ScriptWriteTools,
            "ha_config_set_script",
            source_module="tools_config_scripts",
            annotations=write_hints(
                "Create or Update Script",
                destructive=True,
                idempotent=False,
                open_world=False,
            ),
            tags=frozenset({"Scripts"}),
        ),
        define_tool(
            backend,
            HelperListingTools,
            "ha_config_list_helpers",
            source_module="tools_config_helpers",
            annotations=write_hints(
                "List Helpers", destructive=True, idempotent=False, open_world=False
            ),
            tags=frozenset({"Helper Entities"}),
            writes=description_starts_flow,
            meta={
                "ha_mcp": {
                    "native_support": {
                        "note": "Listing and storage descriptions are read-only. Describing a config/options flow starts Core code and requires write admission."
                    }
                }
            },
        ),
        *(
            define_tool(
                backend,
                owner,
                name,
                source_module=module,
                annotations=write_hints(
                    title, destructive=True, idempotent=True, open_world=False
                ),
                tags=frozenset({tag}),
            )
            for owner, name, module, title, tag in (
                (
                    AutomationRemovalTools,
                    "ha_config_remove_automation",
                    "tools_config_automations",
                    "Remove Automation",
                    "Automations",
                ),
                (
                    ScriptRemovalTools,
                    "ha_config_remove_script",
                    "tools_config_scripts",
                    "Remove Script",
                    "Scripts",
                ),
                (
                    SceneRemovalTools,
                    "ha_config_remove_scene",
                    "tools_config_scenes",
                    "Remove Scene",
                    "Scenes",
                ),
            )
        ),
        define_tool(
            backend,
            ZoneWriteTools,
            "ha_set_zone",
            source_module="tools_zones",
            annotations=write_hints(
                "Set Zone", destructive=True, idempotent=False, open_world=False
            ),
            tags=frozenset({"Zones"}),
        ),
        define_tool(
            backend,
            ZoneWriteTools,
            "ha_remove_zone",
            source_module="tools_zones",
            annotations=write_hints(
                "Remove Zone", destructive=True, idempotent=True, open_world=False
            ),
            tags=frozenset({"Zones"}),
        ),
        define_tool(
            backend,
            StateTools,
            "ha_get_state",
            source_module="tools_search",
            annotations=read_only_hints("Get Entity State", open_world=False),
            tags=frozenset({"Search & Discovery"}),
        ),
        define_tool(
            backend,
            ServiceDiscoveryTools,
            "ha_list_services",
            source_module="tools_services",
            annotations=read_only_hints("List Available Services", open_world=False),
            tags=frozenset({"Service & Device Control"}),
        ),
        define_tool(
            backend,
            AreaDiscoveryTools,
            "ha_list_floors_areas",
            source_module="tools_areas",
            annotations=read_only_hints("List Floors and Areas", open_world=False),
            tags=frozenset({"Areas & Floors"}),
        ),
        define_tool(
            backend,
            ExposureTools,
            "ha_get_entity_exposure",
            source_module="tools_voice_assistant",
            annotations=read_only_hints("Get Entity Exposure", open_world=False),
            tags=frozenset({"Entity Registry"}),
        ),
        define_tool(
            backend,
            ServiceCallTools,
            "ha_call_service",
            source_module="tools_service",
            annotations=write_hints(
                "Call Service", destructive=True, idempotent=False, open_world=False
            ),
            tags=frozenset({"Service & Device Control"}),
        ),
        define_tool(
            backend,
            LabelDiscoveryTools,
            "ha_config_get_label",
            source_module="tools_labels",
            annotations=read_only_hints("Get Label", open_world=False),
            tags=frozenset({"Labels & Categories"}),
        ),
        define_tool(
            backend,
            CategoryDiscoveryTools,
            "ha_config_get_category",
            source_module="tools_categories",
            annotations=read_only_hints("Get Category", open_world=False),
            tags=frozenset({"Labels & Categories"}),
        ),
        define_tool(
            backend,
            DeviceDiscoveryTools,
            "ha_get_device",
            source_module="tools_registry",
            annotations=read_only_hints(
                "Get Device (incl. Zigbee/ZHA/Z2M, Z-Wave and Matter)", open_world=False
            ),
            tags=frozenset({"Device Registry", "Zigbee", "Z-Wave", "Matter"}),
        ),
        define_tool(
            backend,
            EntityDiscoveryTools,
            "ha_get_entity",
            source_module="tools_entities",
            annotations=read_only_hints("Get Entity", open_world=False),
            tags=frozenset({"Entity Registry"}),
        ),
        define_tool(
            backend,
            SkillGuideTools,
            "ha_get_skill_guide",
            source_module="server",
            annotations=read_only_hints(
                "Get Home Assistant Best Practices Skill Guide", open_world=False
            ),
            tags=frozenset({"System"}),
        ),
        define_tool(
            backend,
            HelperConfigTools,
            "ha_config_set_helper",
            source_module="tools_config_helpers",
            annotations=write_hints(
                "Create or Update Helper",
                destructive=True,
                idempotent=False,
                open_world=False,
            ),
            tags=frozenset({"Helper Entities"}),
            meta={
                "ha_mcp": {
                    "native_support": {
                        "storage_helpers": {
                            "create": sorted(SIMPLE_HELPER_TYPES),
                            "update": sorted(SIMPLE_HELPER_TYPES),
                        },
                        "flow_helpers": {
                            "create": "Home Assistant's discovered helper flows",
                            "update": "Options flows of existing helper entries",
                        },
                        "config_subentries": True,
                        "note": "Native helper storage, config/options flows and config subentries. External authorization and progress steps require the Home Assistant UI, as on the remote backend.",
                    }
                }
            },
        ),
        define_tool(
            backend,
            AutomationReadTools,
            "ha_config_get_automation",
            source_module="tools_config_automations",
            annotations=read_only_hints("Get Automation Config", open_world=False),
            tags=frozenset({"Automations"}),
        ),
        define_tool(
            backend,
            ScriptReadTools,
            "ha_config_get_script",
            source_module="tools_config_scripts",
            annotations=read_only_hints("Get Script Config", open_world=False),
            tags=frozenset({"Scripts"}),
        ),
        define_tool(
            backend,
            ZoneReadTools,
            "ha_get_zone",
            source_module="tools_zones",
            annotations=read_only_hints("Get Zone", open_world=False),
            tags=frozenset({"Zones"}),
        ),
        define_tool(
            backend,
            SceneReadTools,
            "ha_config_get_scene",
            source_module="tools_config_scenes",
            annotations=read_only_hints("Get or Find Scenes", open_world=False),
            tags=frozenset({"Scenes"}),
        ),
    ]
