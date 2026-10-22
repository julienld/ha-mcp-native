"""
Search and discovery tools for Home Assistant MCP server.

This module provides entity search, system overview, deep search, and state retrieval tools.
"""

from typing import Annotated, Any, Literal, cast

from pydantic import Field

from ha_mcp._vendor.fastmcp import Context
from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import legacy_errors
from ..config import get_global_settings
from ..errors import create_validation_error
from ..runtime.overview_tools import OverviewService, OverviewTools
from ..runtime.overview_values import resolve_overview_request
from ..transforms.categorized_search import DEFAULT_PINNED_TOOLS
from .coercion import JSON_STRING_COERCION, parse_string_list_param
from .component_api import (
    DEVICE_REGISTRY_CHILD_SEMANTICS,
    component_supports,
    get_component_caps,
)
from .helpers import (
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .response_helpers import project_fields
from .search.component import ComponentSearchMixin, _component_serves_search_types
from .search.entities import _requested_membership, _validate_result_field_names
from .search.overview import (
    _OVERVIEW_AVAILABLE_FIELDS,
    _OVERVIEW_INDEPENDENT_FIELDS,
    OverviewMixin,
)
from .search.response import (
    _compute_eligibility,
    _ResolvedSearch,
    _validate_search_types,
)
from .search.state import StateMixin
from .tool_hints import read_only_hints


class SearchTools(ComponentSearchMixin, OverviewMixin, StateMixin):
    """Tool class providing search and entity discovery capabilities."""

    def __init__(self, client: Any, smart_tools: Any) -> None:
        self._client = client
        self._smart_tools = smart_tools

    @tool(
        name="ha_search",
        tags={"Search & Discovery"},
        annotations=read_only_hints("Search", open_world=False),
    )
    @log_tool_usage
    async def ha_search(
        self,
        query: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "What to search for (entity name fragment, free-text "
                    "config term, entity_id). "
                    "Pass the exact entity_id, not a name fragment, when "
                    "checking what a rename or delete would break: that form "
                    "reports automations, scripts and scenes referencing it "
                    "even when their configuration could not be read. "
                    "Omit `query` to enumerate by `domain_filter`, "
                    "`area_filter`, and/or `state_filter` alone "
                    "(registry-listing mode); configuration-body search is "
                    "skipped in that mode because there is no term to match "
                    "against."
                ),
            ),
        ] = None,
        domain_filter: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Narrow entity-registry results to a single domain "
                    "(e.g. 'light', 'sensor')."
                ),
            ),
        ] = None,
        area_filter: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Narrow entity-registry results to an area (id, name, or alias), "
                    "an exact floor (id, name, or alias), or an unambiguous "
                    "close-spelling floor match; a floor match expands to all areas "
                    "on that floor."
                ),
            ),
        ] = None,
        search_types: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "Configuration types to include in body search: 'automation', 'script',"
                    " 'scene', 'helper', 'dashboard'. Explicitly providing this selects "
                    "configuration-only search and skips entities. Omit it for entity "
                    "discovery. Default = automation+script+scene+helper."
                ),
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(
                default=10,
                ge=1,
                description=("Maximum results per surface (entities, configs)."),
            ),
        ] = 10,
        offset: Annotated[
            int,
            Field(
                default=0,
                ge=0,
                description="Number of results to skip for pagination.",
            ),
        ] = 0,
        exact_match: Annotated[
            bool,
            Field(
                default=True,
                description=(
                    "Exact substring matching. Set False for fuzzy matching when the query "
                    "may have typos."
                ),
            ),
        ] = True,
        include_hidden: Annotated[
            bool,
            Field(
                default=True,
                description=(
                    "Include hidden entities in registry results (with a "
                    "score penalty so they sort below visible matches). "
                    "Set False to exclude entirely."
                ),
            ),
        ] = True,
        include_config: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Include full configuration bodies in body-search results. Otherwise "
                    "summaries only."
                ),
            ),
        ] = False,
        group_by_domain: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Group entity-registry results by domain (entity-side only). "
                    "Adds a `by_domain` map to the response."
                ),
            ),
        ] = False,
        per_domain_limit: Annotated[
            int | None,
            Field(
                default=None,
                description=(
                    "When `group_by_domain=True`, cap entity-registry results "
                    "per domain to this number. Ignored otherwise."
                ),
            ),
        ] = None,
        state_filter: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Filter entity-registry results to a specific state "
                    '(e.g. "on", "off", "unavailable"). Case-insensitive. '
                    "Can be used standalone (no query/domain/area) to enumerate "
                    "every entity in that state; entity_total_matches reflects "
                    "the filtered count."
                ),
            ),
        ] = None,
        result_fields: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "Project each entity-registry record to only the specified "
                    'keys (e.g. ["entity_id", "state"]). None = full records. '
                    "Base keys: entity_id, friendly_name, domain, state, score, "
                    "match_type. Opt-in enrichment/membership keys (computed on request): "
                    "area, floor, labels, aliases, is_group, member_entity_ids. "
                    "Membership is recognized only when HA explicitly exposes a "
                    "valid group_entities or legacy entity_id collection; "
                    "member IDs are sorted, direct (not recursively expanded), "
                    "and omitted if visibility/include_hidden excludes a member. "
                    "Requesting member_entity_ids also retains is_group. "
                    "An unknown key is rejected."
                ),
            ),
        ] = None,
        fields: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "Project the response to the named top-level keys "
                    '(e.g. ["entities", "automations"]); None = full '
                    "response. Diagnostic / pagination keys are always "
                    "retained so projection cannot hide partial / error "
                    "state. Distinct from `result_fields` (which projects "
                    "each entity record's keys). Available keys: success, "
                    "query, entities, automations, scripts, scenes, "
                    "helpers, dashboards, search_types, search_type, "
                    "entity_total_matches, config_total_matches, count, "
                    "offset, limit, has_more, next_offset, "
                    "entity_has_more, entity_next_offset, "
                    "config_has_more, config_next_offset, by_domain, "
                    "state_filter_note, area_names, domain_filter, "
                    "area_filter, message, warnings, errors, partial, "
                    "partial_reason."
                ),
            ),
        ] = None,
        config_time_budget: Annotated[
            float | None,
            Field(
                default=None,
                # Inclusive floor, not gt=0. Home Assistant re-emits this
                # schema for a conversation agent through an OpenAPI 3.0
                # codec, which turns an exclusive bound into a form Anthropic
                # rejects as an invalid input_schema — failing every turn, not
                # just calls to this tool. See tests/src/unit/
                # test_tool_schema_exclusive_bounds.py (issue #2361).
                ge=0.001,
                le=300,
                description=(
                    "Per-call override for the per-id config-fetch wall-clock budget "
                    "(seconds). Replaces the per-type HAMCP_*_CONFIG_TIME_BUDGET defaults "
                    "for the automation, script, AND scene branches. Use when a `partial: "
                    "True` response names time-budget skipping. None = use the per-type env"
                    " defaults."
                ),
            ),
        ] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Search for entities (lights, sensors, switches, climate, etc.) by name, domain, or area — AND inside automation/script/scene/helper/dashboard configurations — in one call.

        Two surfaces run in parallel and return tagged results:
          - **entities**: entity-registry matches (entity_id, friendly name,
            area). Filter with `domain_filter`/`area_filter`/`state_filter`.
          - **automations / scripts / scenes / helpers / dashboards**: matches
            *inside* config definitions — triggers, actions, sequences, scene
            entity-sets, helper bodies, dashboard cards. Driven by `query`;
            narrow with `search_types`.

        Use dedicated get/list tools first for a known resource type, including
        ha_config_get_scene for scene listing and content search. Use this for
        broader discovery or deep searches across resource types.

        For control requests with exclusions such as "except", "excluding", or
        "but not", include `is_group` and `member_entity_ids` in `result_fields`.
        Do not control an aggregate whose members include an excluded entity;
        prefer leaf entities when the exception cannot be verified safely.
        A withheld member list still returns is_group=true; absence of
        member_entity_ids must not be interpreted as a leaf entity.

        When NOT to use:
          - To read a known entity_id's state: use `ha_get_state` (cheaper).
          - To inspect one automation/script/scene config by id: use the
            matching `ha_config_get_*`.
          - To list installed Apps (add-ons): use `ha_get_app`.

        Config-body search is skipped when `domain_filter`/`area_filter`/
        `state_filter` signal entity-only intent (keeping name lookups off the
        expensive backend); a `warnings[]` entry names the skip. Repeat without
        entity filters to search configuration contents too.

        Caveats:
          - `partial: True` means results are NOT exhaustive — a surface raised,
            or the config-body branch lost data (per-id time budget exhausted,
            an individual fetch failed, or a helper-type list fetch failed).
            Empty buckets with `partial: True` mean "search failed", not "no
            results". The cause is in `partial_reason`, also mirrored into
            `warnings[]` with an "incomplete results: " prefix. Do not treat a
            partial response as complete.
          - `count` is items in this response (post-pagination), not corpus
            totals — use `entity_total_matches` + `config_total_matches`.
          - `limit`/`offset` apply per-surface. Flat `has_more`/`next_offset`
            page the next call (iterate `offset = next_offset`); per-surface
            `entity_*`/`config_*` variants show which surface still has results.

        Examples:
            - Find a light by name: ha_search("kitchen", domain_filter="light")
            - List sensors in an area: ha_search(domain_filter="sensor", area_filter="Living Room")
            - Find lights safely before an "all except one" control request:
              ha_search("living room", domain_filter="light",
              result_fields=["entity_id", "friendly_name", "is_group",
              "member_entity_ids"])
            - Which automations use an entity: ha_search("light.bed_light")
            - All unavailable entities: ha_search(state_filter="unavailable")
        """
        try:
            parsed_search_types = parse_string_list_param(search_types, "search_types")
        except ValueError as exc:
            raise_tool_error(
                create_validation_error(str(exc), parameter="search_types")
            )
        _validate_search_types(parsed_search_types)
        try:
            parsed_fields = parse_string_list_param(fields, "fields", allow_csv=True)
        except ValueError as exc:
            raise_tool_error(create_validation_error(str(exc), parameter="fields"))

        # Validate result_fields once up front so BOTH serving paths reject an
        # unknown enrichment key identically (the sub-paths re-parse the same raw
        # value for their own projection).
        try:
            parsed_result_fields = parse_string_list_param(
                result_fields, "result_fields", allow_csv=True
            )
        except ValueError as exc:
            raise_tool_error(
                create_validation_error(str(exc), parameter="result_fields")
            )
        _validate_result_field_names(parsed_result_fields)

        # Normalise the caller-input strings once; the eligibility helper
        # below is purely a function of normalized inputs so it stays
        # unit-testable without an MCP fixture.
        query_text = (query or "").strip()
        domain_filter_text = (domain_filter or "").strip()
        area_filter_text = (area_filter or "").strip()
        state_filter_text = (state_filter or "").strip()
        explicit_config_only = parsed_search_types is not None
        registry_eligible, body_eligible, body_skipped_by_intent_gate = (
            _compute_eligibility(
                query_text=query_text,
                domain_filter_text=domain_filter_text,
                area_filter_text=area_filter_text,
                state_filter_text=state_filter_text,
                explicit_config_only=explicit_config_only,
            )
        )

        if not registry_eligible and not body_eligible:
            raise_tool_error(
                create_validation_error(
                    "ha_search requires a non-empty query, or one of "
                    "domain_filter / area_filter / state_filter to enumerate.",
                    parameter="query",
                )
            )

        req = _ResolvedSearch(
            query=query,
            query_text=query_text,
            domain_filter=domain_filter,
            area_filter=area_filter,
            state_filter=state_filter,
            parsed_search_types=parsed_search_types,
            parsed_fields=parsed_fields,
            result_fields=result_fields,
            limit=limit,
            offset=offset,
            exact_match=exact_match,
            include_hidden=include_hidden,
            include_config=include_config,
            group_by_domain=group_by_domain,
            per_domain_limit=per_domain_limit,
            config_time_budget=config_time_budget,
            registry_eligible=registry_eligible,
            body_eligible=body_eligible,
            body_skipped_by_intent_gate=body_skipped_by_intent_gate,
        )

        # Capability-gated semantics keep old components compatible while newer
        # components serve queryless listings, locations, and complete windows.
        # Visibility and membership gates still apply before any entity data is
        # returned; schema hiding never changes old-client invocation support.
        if _component_serves_search_types(req):
            caps = await get_component_caps(self._client)
            mode_supported = component_supports(caps, "search_unified") or (
                bool(req.query_text) and not (req.area_filter or "").strip()
            )
            if (
                mode_supported
                and component_supports(caps, "search")
                and component_supports(caps, DEVICE_REGISTRY_CHILD_SEMANTICS)
                and (
                    not _requested_membership(parsed_result_fields)
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
