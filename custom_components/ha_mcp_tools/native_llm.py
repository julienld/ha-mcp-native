"""Expose the native catalogue to Assist without an internal MCP connection."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm

from .const import (
    DATA_LLM_API_UNSUB,
    DEFAULT_LLM_API_EXPOSURE,
    DOMAIN,
    EXPOSURE_BOTH,
    EXPOSURE_FULL,
    EXPOSURE_TOOL_SEARCH,
    OPT_LLM_API_EXPOSURE,
)
from .llm_api import _FALLBACK_API_PROMPT, _TOOL_SEARCH_PROMPT, HaMcpLlmApi
from .llm_api_search import CALL_TOOL_NAME, SEARCH_TOOL_NAME
from .llm_tool_exposure import partition_tools
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

    from .native_server_policy import filter_catalogue

    await manager.refresh_policy()
    overrides, pinned = manager.policy.llm_overrides, set(manager.policy.pinned)
    tools = filter_catalogue(manager, await manager.mcp.local_provider._list_tools())
    exposed = []
    for tool in tools:
        # Importing our own exported API would create a recursive bridge.
        if tool.name.startswith("ha_native_llm_"):
            continue
        if effective_llm_api_exposed(tool.name, tool.tags, overrides):
            exposed.append(tool)
    return exposed, pinned


async def call_exposed(
    manager: NativeServerManager,
    name: str,
    arguments: dict[str, Any],
    context: llm.LLMContext,
) -> Any:
    """Recompute exposure at execution, including already advertised tools."""
    from ha_mcp.renamed_tools import adapt_retired_arguments, current_tool_name
    from ha_mcp.tool_api import ToolFailure, ToolResult

    from .llm_api import _meta_as_field_name
    from .native_server import mcp_result

    actor = context_from_llm(context)
    arguments = adapt_retired_arguments(name, arguments) or {}
    name = current_tool_name(name)
    await manager.backend.authorize(actor)
    tools, _ = await exposed_catalog(manager)
    names = frozenset(tool.name for tool in tools)
    if name not in names:
        return tool_result({"error": f"Unknown tool '{name}'."}, error=True)
    try:
        result = await manager.engine.call(
            name, arguments, replace(actor, allowed_tools=names)
        )
    except ToolFailure as err:
        result = ToolResult(err.data, is_error=True, status=err.status or "not_started")
    wire = mcp_result(result, allow_continuation=False).to_mcp_result()
    payload = _meta_as_field_name(
        wire.model_dump(exclude_unset=True, exclude_none=True, by_alias=True)
    )
    return tool_result(payload, error=result.is_error)


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
    ) -> Any:
        return await call_exposed(
            self.manager, self.name, tool_input.tool_args, llm_context
        )


class NativeCallTool(llm.Tool):
    """Call only a currently exposed native tool discovered through search."""

    def __init__(self, manager: NativeServerManager) -> None:
        from .llm_api import HaMcpCallTool

        self.manager = manager
        self.name = CALL_TOOL_NAME
        self.description = HaMcpCallTool.description
        self.parameters = HaMcpCallTool.parameters
        declare_metadata(self, title="Call HA-MCP Tool", hints=None)

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> Any:
        return await call_exposed(
            self.manager,
            str(tool_input.tool_args.get("name", "")),
            tool_input.tool_args.get("arguments") or {},
            llm_context,
        )


@dataclass(kw_only=True)
class NativeLlmApi(HaMcpLlmApi):
    """Keep established LLM API IDs and search/schema behavior with direct calls."""

    manager: Any
    requires_admin: bool = True

    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        from types import SimpleNamespace

        actor = context_from_llm(llm_context)
        try:
            await self.manager.backend.authorize(actor)
        except PermissionError as err:
            raise HomeAssistantError(
                "HA-MCP requires an identified, active administrator"
            ) from err
        exposed, pinned = await exposed_catalog(self.manager)
        # Existing conversion/search code consumes MCP wire names; adapt just
        # those attributes, preserving the catalogue's schema without a wire trip.
        descriptors = [
            SimpleNamespace(
                name=t.name,
                description=t.description,
                title=t.title,
                annotations=t.annotations,
                input_schema=t.parameters,
                meta={"ha_mcp": {"llm_api_exposed": True, "pinned": t.name in pinned}},
            )
            for t in exposed
            if t.name not in (SEARCH_TOOL_NAME, CALL_TOOL_NAME)
        ]
        exposed, pinned, _ = partition_tools(descriptors)
        prompt = self.manager.mcp.instructions or _FALLBACK_API_PROMPT
        if self.mode == EXPOSURE_FULL:
            tools = self._build_full_tools(exposed)
        else:
            tools = self._build_tool_search_tools(exposed, pinned)
            tools = [t for t in tools if t.name != CALL_TOOL_NAME]
            tools.append(NativeCallTool(self.manager))
            prompt += _TOOL_SEARCH_PROMPT
        return llm.APIInstance(self, prompt, llm_context, tools)

    def _mirror(self, tool: Any, parameters: Any) -> NativeTool:
        return NativeTool(self.manager, tool, parameters)


async def async_register_native_llm(
    hass: HomeAssistant, entry: ConfigEntry, manager: NativeServerManager
) -> None:
    """Preserve full and tool-search API IDs selected in existing agents."""
    from .llm_api import _schema_converter, async_unregister_llm_api

    await hass.async_add_executor_job(_schema_converter)
    async_unregister_llm_api(hass)
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
                server_url="",
                mode=mode,
                manager=manager,
            )
            unsubs.append(llm.async_register_api(hass, api))
    except BaseException:
        for unsub in unsubs:
            unsub()
        raise
    hass.data.setdefault(DOMAIN, {})[DATA_LLM_API_UNSUB] = unsubs
