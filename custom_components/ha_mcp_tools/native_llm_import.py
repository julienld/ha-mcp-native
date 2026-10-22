"""Import explicitly selected Core LLM APIs through a bounded native bridge."""

from __future__ import annotations

import json
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, cast

from homeassistant.core import Context
from homeassistant.helpers import llm
from homeassistant.helpers.json import json_bytes

from .const import DOMAIN, OPT_NATIVE_IMPORT_APIS

if TYPE_CHECKING:
    from ha_mcp.tool_api import CallContext

    from .native_server import NativeServerManager

_IMPORT_STACK: ContextVar[tuple[str, ...]] = ContextVar(
    "ha_mcp_llm_import_stack", default=()
)


def selected_apis(manager: NativeServerManager) -> frozenset[str]:
    raw = manager.entry.options.get(OPT_NATIVE_IMPORT_APIS, [])
    return frozenset(
        item for item in raw if isinstance(item, str) and not item.startswith(DOMAIN)
    )


async def resolve_api(
    manager: NativeServerManager, api_id: str, actor: CallContext
) -> Any:
    """Core remains the API owner; resolve a fresh authorized instance per call."""
    await manager.backend.authorize(actor)
    if api_id not in selected_apis(manager) or api_id in _IMPORT_STACK.get():
        raise PermissionError("LLM API is not selected or would create an import loop")
    context = llm.LLMContext(
        platform=DOMAIN,
        context=Context(user_id=actor.user_id),
        language=actor.language,
        assistant="conversation",
        device_id=None,
    )
    instance = await llm.async_get_api(manager.hass, api_id, context)
    if instance.api.id.startswith(DOMAIN):
        raise PermissionError("Cannot import an HA-MCP exported API")
    return instance


def _schema(instance: Any, tool: Any) -> dict[str, Any]:
    """Serialize the owning API's validators without replacing them."""
    import voluptuous as vol

    if isinstance(tool.parameters, (vol.Schema, dict)):
        from voluptuous_openapi import convert

        return cast(
            dict[str, Any],
            convert(tool.parameters, custom_serializer=instance.custom_serializer),
        )
    import probatio

    return probatio.to_openapi(
        tool.parameters, custom_serializer=instance.custom_serializer
    )


def _writes(tool: Any) -> bool:
    return getattr(getattr(tool, "annotations", None), "read_only", False) is not True


async def inspect_api(
    manager: NativeServerManager, api_id: str, actor: CallContext
) -> dict[str, Any]:
    instance = await resolve_api(manager, api_id, actor)
    return {
        "api_id": api_id,
        "instructions": instance.api_prompt,
        "tools": [
            {
                "name": f"{api_id}/{tool.name}",
                "owner": getattr(tool, "integration", None),
                "description": tool.description,
                "inputSchema": _schema(instance, tool),
                "readOnlyHint": not _writes(tool),
            }
            for tool in instance.tools
        ],
    }


async def call_imported(
    manager: NativeServerManager,
    qualified_name: str,
    arguments: dict[str, Any],
    actor: CallContext,
) -> Any:
    """Revalidate owner, availability and original schema immediately before use."""
    from ha_mcp.tool_api import ToolResult

    api_id, separator, tool_name = qualified_name.partition("/")
    if not separator or not api_id or not tool_name:
        raise ValueError("Use the API/tool name returned by ha_native_llm_tools")
    instance = await resolve_api(manager, api_id, actor)
    advertised = next((tool for tool in instance.tools if tool.name == tool_name), None)
    if advertised is None:
        raise ValueError("Unknown LLM tool")
    owner = getattr(advertised, "integration", None)
    writes = _writes(advertised)

    async def invoke() -> ToolResult:
        actual = await resolve_api(manager, api_id, actor)
        tool = next((tool for tool in actual.tools if tool.name == tool_name), None)
        if (
            tool is None
            or getattr(tool, "integration", None) != owner
            or _writes(tool) != writes
        ):
            raise PermissionError("LLM tool owner or capabilities changed")
        # APIInstance historically validated parameters, but current Core does
        # not. Calling the original validator supports both without a copy.
        validated = tool.parameters(dict(arguments))
        stack = _IMPORT_STACK.set((*_IMPORT_STACK.get(), api_id))
        try:
            result = await actual.async_call_tool(
                llm.ToolInput(tool_name=tool_name, tool_args=validated, external=True)
            )
        finally:
            _IMPORT_STACK.reset(stack)
        data = getattr(result, "data", result)
        return ToolResult(
            json.loads(json_bytes(data)), is_error=bool(getattr(result, "error", False))
        )

    return await manager.engine.call_owned(
        "ha_native_llm_call", invoke, actor, writes=writes
    )


def register_import_tools(manager: NativeServerManager) -> None:
    """Discovery and calls share API-qualified names and preserve owner prompts."""
    from .native_server import current_context, mcp_result

    if not selected_apis(manager):
        return

    @manager.mcp.tool(
        annotations={
            "title": "Inspect selected Home Assistant LLM API",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    async def ha_native_llm_tools(api_id: str) -> dict[str, Any]:
        """Read instructions and API-qualified tools from a selected native API."""
        return await inspect_api(manager, api_id, current_context())

    @manager.mcp.tool(
        annotations={
            "title": "Call selected Home Assistant LLM API tool",
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )
    async def ha_native_llm_call(name: str, arguments: dict[str, Any]) -> Any:
        """Call an API/tool discovered with ha_native_llm_tools; follow its instructions."""
        return mcp_result(
            await call_imported(manager, name, arguments, current_context())
        )
