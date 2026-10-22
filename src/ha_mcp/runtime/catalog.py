"""Project shared Python tool signatures into the neutral catalog."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, get_type_hints

from pydantic import ConfigDict, ValidationError, create_model

from ha_mcp.tool_api import Backend, CallContext, ToolDefinition, ToolFailure
from ha_mcp.tools.tool_hints import read_only_hints

from .services import ServiceDiscoveryTools
from .states import StateTools


def define_tool(
    backend: Backend,
    owner: type[Any],
    method_name: str,
    *,
    annotations: dict[str, Any],
    tags: frozenset[str] = frozenset(),
) -> ToolDefinition:
    """One signature drives validation, schemas and both transport facades."""
    method = getattr(owner, method_name)
    signature = inspect.signature(method)
    hints = get_type_hints(method, include_extras=True)
    fields = {
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

    return ToolDefinition(
        method_name,
        inspect.getdoc(method) or "",
        model.model_json_schema(),
        invoke,
        annotations,
        validate=validate,
        tags=tags,
    )


def build_initial_catalog(backend: Backend) -> list[ToolDefinition]:
    """Only complete migrated tools are advertised by the native entry."""
    return [
        define_tool(
            backend,
            StateTools,
            "ha_get_state",
            annotations=read_only_hints("Get Entity State", open_world=False),
            tags=frozenset({"Search & Discovery"}),
        ),
        define_tool(
            backend,
            ServiceDiscoveryTools,
            "ha_list_services",
            annotations=read_only_hints("List Available Services", open_world=False),
            tags=frozenset({"Service & Device Control"}),
        ),
    ]
