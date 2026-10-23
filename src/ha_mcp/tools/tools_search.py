"""
Search and discovery tools for Home Assistant MCP server.

This module provides entity search, system overview, deep search, and state retrieval tools.
"""

from typing import Any, Literal, cast

from ha_mcp._vendor.fastmcp import Context
from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import LegacyBackendTools, legacy_errors
from ..config import get_global_settings
from ..runtime.overview_tools import OverviewService, OverviewTools
from ..runtime.overview_values import resolve_overview_request
from ..runtime.search_request import resolve_search_request
from ..runtime.search_tools import SearchFacade, SearchService
from ..transforms.categorized_search import DEFAULT_PINNED_TOOLS
from .coercion import parse_string_list_param
from .component_api import (
    DEVICE_REGISTRY_CHILD_SEMANTICS,
    component_supports,
    get_component_caps,
)
from .helpers import (
    log_tool_usage,
    register_tool_methods,
)
from .response_helpers import project_fields
from .search.component import ComponentSearchMixin, _component_serves_search_types
from .search.entities import _requested_membership
from .search.overview import (
    _OVERVIEW_AVAILABLE_FIELDS,
    _OVERVIEW_INDEPENDENT_FIELDS,
    OverviewMixin,
)
from .search.state import StateMixin
from .tool_hints import read_only_hints


class SearchTools(LegacyBackendTools, ComponentSearchMixin, OverviewMixin, StateMixin):
    """Tool class providing search and entity discovery capabilities."""

    def __init__(self, client: Any, smart_tools: Any) -> None:
        super().__init__(client)
        self._smart_tools = smart_tools

    @property
    def searcher(self) -> SearchService:
        return cast(SearchService, self)

    async def search(
        self,
        query: str | None = None,
        domain_filter: str | None = None,
        area_filter: str | None = None,
        search_types: str | list[str] | None = None,
        limit: int = 10,
        offset: int = 0,
        exact_match: bool = True,
        include_hidden: bool = True,
        include_config: bool = False,
        group_by_domain: bool = False,
        per_domain_limit: int | None = None,
        state_filter: str | None = None,
        result_fields: str | list[str] | None = None,
        fields: str | list[str] | None = None,
        config_time_budget: float | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        if ctx is None:
            from ha_mcp._vendor.fastmcp.server.dependencies import get_context

            try:
                ctx = get_context()
            except RuntimeError:
                ctx = None
        req = resolve_search_request(
            {
                "query": query,
                "domain_filter": domain_filter,
                "area_filter": area_filter,
                "search_types": search_types,
                "limit": limit,
                "offset": offset,
                "exact_match": exact_match,
                "include_hidden": include_hidden,
                "include_config": include_config,
                "group_by_domain": group_by_domain,
                "per_domain_limit": per_domain_limit,
                "state_filter": state_filter,
                "result_fields": result_fields,
                "fields": fields,
                "config_time_budget": config_time_budget,
            }
        )
        if _component_serves_search_types(req):
            caps = await get_component_caps(self._client)
            mode_supported = component_supports(caps, "search_unified") or (
                bool(req.query_text) and (not (req.area_filter or "").strip())
            )
            if (
                mode_supported
                and component_supports(caps, "search")
                and component_supports(caps, DEVICE_REGISTRY_CHILD_SEMANTICS)
                and (
                    not _requested_membership(
                        parse_string_list_param(
                            result_fields, "result_fields", allow_csv=True
                        )
                    )
                    or component_supports(caps, "search_entity_membership")
                )
            ):
                (
                    route_component,
                    visibility,
                ) = await self._resolve_component_search_visibility(caps)
                if route_component:
                    component_response = await self._ha_search_via_component(
                        req, ctx, visibility=visibility, caps=caps
                    )
                    if component_response is not None:
                        return component_response
        return await self._legacy_ha_search(req, ctx)

    @property
    def overview(self) -> OverviewService:
        return cast(OverviewService, self)

    async def get_overview(
        self,
        detail_level: Literal["minimal", "standard", "full"] = "minimal",
        domains: str | list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        max_entities_per_domain: int | None = None,
        include_state: bool | None = None,
        include_entity_id: bool | None = None,
        include_notifications: bool | None = True,
        include_dismissed_repairs: bool | None = False,
        fields: str | list[str] | None = None,
    ) -> dict[str, Any]:
        inputs, parsed_fields, independent = resolve_overview_request(
            detail_level=detail_level,
            domains=domains,
            limit=limit,
            offset=offset,
            max_entities_per_domain=max_entities_per_domain,
            include_state=include_state,
            include_entity_id=include_entity_id,
            include_notifications=include_notifications,
            include_dismissed_repairs=include_dismissed_repairs,
            fields=fields,
        )
        if independent:
            result = await self._collect_independent_overview(
                requested_fields=set(parsed_fields or [])
                & _OVERVIEW_INDEPENDENT_FIELDS,
                detail_level=inputs.detail_level,
                include_notifications=inputs.include_notifications,
                include_dismissed_repairs=inputs.include_dismissed_repairs,
            )
        else:
            result = await self._collect_overview(inputs)
        settings = get_global_settings()
        if settings.enable_tool_search:
            result["tool_discovery"] = {
                "hint": "This server uses search-based tool discovery. Use ha_search_tools(query='...') to find tools, ha_search_tools(tools=['<name>']) for the full input schema of the one you will call, then execute the discovered tool directly by name (preferred), or via a proxy for permission gating: ha_call_read_tool, ha_call_write_tool, or ha_call_delete_tool. Each proxy takes name and arguments as separate top-level params. Call proxy tools SEQUENTIALLY (not in parallel) to avoid cascading cancellations. Do NOT assume a capability is unavailable without searching first.",
                "pinned_tools": sorted(
                    [
                        *DEFAULT_PINNED_TOOLS,
                        "ha_search_tools",
                        "ha_call_read_tool",
                        "ha_call_write_tool",
                        "ha_call_delete_tool",
                    ]
                ),
            }
        from ..stdio_settings_sidecar import read_sidecar_url

        projected = project_fields(
            result, parsed_fields, available_fields=_OVERVIEW_AVAILABLE_FIELDS
        )
        sidecar_url = read_sidecar_url()
        if sidecar_url:
            projected["settings_url"] = sidecar_url
        else:
            from ..settings_ui import get_http_settings_prefix

            http_prefix = get_http_settings_prefix()
            if http_prefix:
                settings_path = f"{http_prefix.rstrip('/')}/settings"
                projected["settings_url_hint"] = (
                    f"The settings page (enable/disable/pin tools, feature flags, advanced settings, backups, tool-approval) is served at '{settings_path}' on this MCP server. Find the full URL in the ha-mcp startup logs. For a direct connection, use the MCP endpoint's scheme, host, and port with this settings path (replace the endpoint path rather than appending to it)."
                )
        from ..read_only import is_read_only, read_only_remedy_hint

        if is_read_only():
            projected["read_only_mode"] = True
            projected["read_only_mode_hint"] = (
                f"Read Only Mode is ON: write-capable tools are disabled and all write or destructive operations are blocked server-side. You can search, read, and analyze freely. {read_only_remedy_hint()}"
            )
        from ..update_check import get_update_field

        mcp_update = await get_update_field()
        if mcp_update is not None:
            projected["ha_mcp_update"] = mcp_update
        return projected

    ha_search = tool(
        name="ha_search",
        tags={"Search & Discovery"},
        annotations=read_only_hints("Search", open_world=False),
    )(log_tool_usage(legacy_errors(SearchFacade.ha_search)))

    ha_get_overview = tool(
        name="ha_get_overview",
        tags={"Search & Discovery"},
        annotations=read_only_hints("Get System Overview", open_world=True),
    )(log_tool_usage(legacy_errors(OverviewTools.ha_get_overview)))

    ha_get_state = tool(
        name="ha_get_state",
        tags={"Search & Discovery"},
        annotations=read_only_hints("Get Entity State", open_world=False),
    )(log_tool_usage(legacy_errors(StateMixin.ha_get_state)))


def register_search_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register search and discovery tools with the MCP server."""
    smart_tools = kwargs.get("smart_tools")
    if not smart_tools:
        raise ValueError("smart_tools is required for search tools registration")
    register_tool_methods(mcp, SearchTools(client, smart_tools))
