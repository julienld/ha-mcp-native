"""Expose the native catalogue to Assist without an internal MCP connection."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm

from ha_mcp.tool_api.llm_projection import describe_tool, result_envelope

from .const import (
    DATA_LLM_API_UNSUB,
    DEFAULT_LLM_API_EXPOSURE,
    DOMAIN,
    EXPOSURE_BOTH,
    EXPOSURE_FULL,
    EXPOSURE_TOOL_SEARCH,
    OPT_LLM_API_EXPOSURE,
)
from .llm_api_search import CALL_TOOL_NAME
from .llm_bridge_options import export_enabled, require_export_enabled
from .llm_projection import (
    _FALLBACK_API_PROMPT,
    _TOOL_SEARCH_PROMPT,
    CALL_TOOL_DESCRIPTION,
    CALL_TOOL_PARAMETERS,
    _schema_converter,
    async_unregister_llm_api,
    build_full_tools,
    build_search_tools,
    convert_parameters,
)
from .llm_tool_metadata import declare_metadata, tool_hints, tool_result, tool_title

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from ha_mcp.tool_api import CallContext

    from .native_server import NativeServerManager


def context_from_llm(context: llm.LLMContext) -> CallContext:
    """An anonymous Assist turn stays anonymous; there is no admin fallback."""
    from ha_mcp.tool_api import CallContext

    return CallContext(
        user_id=context.context.user_id if context.context else None,
        client_id=f"assist:{context.platform}",
        language=context.language or "en",
    )


async def exposed_catalog(manager: NativeServerManager) -> tuple[list[Any], set[str]]:
    """Share the server's exposure and pinned policy, including its env overrides."""
    from ha_mcp.llm_exposure import effective_llm_api_exposed

    if not export_enabled(manager.entry.options):
        return [], set()
    await manager.refresh_policy()
    if not export_enabled(manager.entry.options):
        return [], set()
    policy = manager.policy
    overrides, pinned = policy.llm_overrides, set(policy.pinned)
    exposed = []
    for definition in manager.engine.tools.values():
        # Importing our own exported API would create a recursive bridge.
        if definition.name.startswith("ha_native_llm_"):
            continue
        writes = not definition.read_only and definition.writes is None
        if (
            policy.tool_enabled(definition)
            and policy.enabled(
                definition.name, writes=writes, source_module=definition.source_module
            )
            and effective_llm_api_exposed(definition.name, definition.tags, overrides)
        ):
            exposed.append(describe_tool(definition))
    return exposed, pinned


async def call_exposed(
    manager: NativeServerManager,
    name: str,
    arguments: dict[str, Any],
    context: llm.LLMContext,
) -> llm.ToolResult:
    """Recompute exposure at execution, including already advertised tools."""
    from ha_mcp.llm_exposure import effective_llm_api_exposed
    from ha_mcp.renamed_tools import adapt_retired_arguments, current_tool_name
    from ha_mcp.tool_api import ToolFailure, ToolResult

    actor = context_from_llm(context)
    arguments = adapt_retired_arguments(name, arguments) or {}
    name = current_tool_name(name)
    await manager.backend.authorize(actor)
    tools, _ = await exposed_catalog(manager)
    names = frozenset(tool.name for tool in tools)
    if name not in names:
        return tool_result({"error": f"Unknown tool '{name}'."}, error=True)

    def check_exposure(tool_name: str) -> None:
        definition = manager.engine.tools.get(tool_name)
        if (
            not export_enabled(manager.entry.options)
            or definition is None
            or not effective_llm_api_exposed(
                tool_name, definition.tags, manager.policy.llm_overrides
            )
        ):
            raise ToolFailure(
                "TOOL_DISABLED", f"Unknown tool '{tool_name}'.", status="not_started"
            )

    try:
        definition = manager.engine.tools.get(name)
        if definition and definition.meta.get("ha_mcp", {}).get(
            "requires_image_content"
        ):
            result = ToolResult(
                {
                    "success": False,
                    "error": {
                        "code": "IMAGE_CONTENT_UNSUPPORTED",
                        "message": "Home Assistant Assist cannot receive tool images. Use an MCP client to retrieve this image.",
                    },
                },
                is_error=True,
                status="not_started",
            )
        else:
            result = await manager.engine.call(
                name,
                arguments,
                replace(actor, allowed_tools=names, tool_guard=check_exposure),
            )
    except ToolFailure as err:
        result = ToolResult(err.data, is_error=True, status=err.status or "not_started")
    if result.images:
        data = (
            dict(result.data)
            if isinstance(result.data, dict)
            else {"result": result.data}
        )
        data["warnings"] = [
            *(data.get("warnings") or []),
            "Home Assistant Assist cannot receive tool images. The structured result and operation outcome are preserved; use an MCP client to receive the screenshot images.",
        ]
        result = replace(result, data=data, images=())
    return tool_result(result_envelope(result), error=result.is_error)


class NativeTool(llm.Tool):
    """One catalogue tool, with Core's schema conversion and original metadata."""

    def __init__(
        self, manager: NativeServerManager, tool: Any, parameters: Any
    ) -> None:
        self.manager = manager
        self.name = tool.name
        self.description = tool.description
        self.parameters = parameters
        declare_metadata(self, title=tool_title(tool), hints=tool_hints(tool))

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> llm.ToolResult:
        return await call_exposed(
            self.manager, self.name, tool_input.tool_args, llm_context
        )


class NativeCallTool(llm.Tool):
    """Call only a currently exposed native tool discovered through search."""

    def __init__(self, manager: NativeServerManager) -> None:
        self.manager = manager
        self.name = CALL_TOOL_NAME
        self.description = CALL_TOOL_DESCRIPTION
        self.parameters = CALL_TOOL_PARAMETERS
        declare_metadata(self, title="Call HA-MCP Tool", hints=None)

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> llm.ToolResult:
        return await call_exposed(
            self.manager,
            str(tool_input.tool_args.get("name", "")),
            tool_input.tool_args.get("arguments") or {},
            llm_context,
        )


@dataclass(kw_only=True)
class NativeLlmApi(llm.API):
    """Keep established LLM API IDs and search/schema behavior with direct calls."""

    manager: Any
    mode: str = EXPOSURE_TOOL_SEARCH
    requires_admin: bool = True

    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        actor = context_from_llm(llm_context)
        try:
            await self.manager.backend.authorize(actor)
        except PermissionError as err:
            raise HomeAssistantError(
                "HA-MCP requires an identified, active administrator"
            ) from err
        self._check_enabled()
        exposed, pinned = await exposed_catalog(self.manager)
        self._check_enabled()
        prompt = self.manager.instructions or _FALLBACK_API_PROMPT
        if self.mode == EXPOSURE_FULL:
            tools = build_full_tools(exposed, self._mirror)
        else:
            tools = build_search_tools(
                exposed,
                pinned,
                self._mirror,
                convert_parameters,
                lambda _names: NativeCallTool(self.manager),
                check_enabled=self._check_enabled,
            )
            prompt += _TOOL_SEARCH_PROMPT
        return llm.APIInstance(self, prompt, llm_context, tools)

    def _check_enabled(self) -> None:
        require_export_enabled(self.manager.entry.options)

    def _mirror(self, tool: Any, parameters: Any) -> NativeTool:
        return NativeTool(self.manager, tool, parameters)


async def async_register_native_llm(
    hass: HomeAssistant, entry: ConfigEntry, manager: NativeServerManager
) -> None:
    """Preserve full and tool-search API IDs selected in existing agents."""
    async_unregister_llm_api(hass)
    if not export_enabled(entry.options):
        return
    await hass.async_add_executor_job(_schema_converter)
    if not export_enabled(entry.options):
        return
    exposure = str(entry.options.get(OPT_LLM_API_EXPOSURE, DEFAULT_LLM_API_EXPOSURE))
    modes = (
        [EXPOSURE_FULL, EXPOSURE_TOOL_SEARCH]
        if exposure == EXPOSURE_BOTH
        else [EXPOSURE_FULL if exposure == EXPOSURE_FULL else EXPOSURE_TOOL_SEARCH]
    )
    unsubs = []
    try:
        for mode in modes:
            search = mode == EXPOSURE_TOOL_SEARCH
            api = NativeLlmApi(
                hass=hass,
                id=f"{DOMAIN}-{entry.entry_id}" + ("-toolsearch" if search else ""),
                name=entry.title + (" (tool search)" if search else ""),
                mode=mode,
                manager=manager,
            )
            unsubs.append(llm.async_register_api(hass, api))
    except BaseException:
        for unsub in unsubs:
            unsub()
        raise
    hass.data.setdefault(DOMAIN, {})[DATA_LLM_API_UNSUB] = unsubs
