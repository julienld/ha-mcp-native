"""Project shared Python tool signatures into the neutral catalog."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, get_type_hints

from pydantic import ConfigDict, TypeAdapter, ValidationError, create_model

from ha_mcp.tool_api import Backend, CallContext, ToolDefinition, ToolFailure
from ha_mcp.tool_api.parameters import hidden_param_names
from ha_mcp.tools.tool_hints import read_only_hints, write_hints

from .actions import ServiceCallTools
from .devices import DeviceDiscoveryTools
from .entities import EntityDiscoveryTools
from .exposure import ExposureTools
from .helper_schema import SIMPLE_HELPER_TYPES
from .helper_tools import HelperConfigTools
from .labels import CategoryDiscoveryTools, LabelDiscoveryTools
from .registries import AreaDiscoveryTools
from .services import ServiceDiscoveryTools
from .skills import SkillGuideTools
from .states import StateTools


def define_tool(
    backend: Backend,
    owner: type[Any],
    method_name: str,
    *,
    annotations: dict[str, Any],
    tags: frozenset[str] = frozenset(),
    source_module: str | None = None,
    meta: dict[str, Any] | None = None,
) -> ToolDefinition:
    """One signature drives validation, schemas and both transport facades."""
    method = getattr(owner, method_name)
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
        invoke,
        annotations,
        output_schema=_without_schema_titles(
            TypeAdapter(hints.get("return", Any)).json_schema()
        ),
        validate=validate,
        tags=tags,
        source_module=source_module,
        meta=meta or {},
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


def build_initial_catalog(backend: Backend) -> list[ToolDefinition]:
    """Only complete migrated tools are advertised by the native entry."""
    return [
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
                        "flow_helpers": {"create": ["utility_meter"], "update": []},
                        "config_subentries": False,
                        "note": "Experimental native branches: storage helper create/update and utility_meter creation. Other helper flows and subentries are not ported yet.",
                    }
                }
            },
        ),
    ]
