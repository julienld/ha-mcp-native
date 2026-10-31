"""Search's single public signature, independent of transport progress contexts."""

from typing import Annotated, Any, Protocol

from pydantic import Field

from ha_mcp.tools.coercion import JSON_STRING_COERCION


class SearchService(Protocol):
    async def search(self, **arguments: Any) -> dict[str, Any]: ...


class SearchFacade:
    def __init__(self, searcher: SearchService) -> None:
        self.searcher = searcher

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
        return await self.searcher.search(
            query=query,
            domain_filter=domain_filter,
            area_filter=area_filter,
            search_types=search_types,
            limit=limit,
            offset=offset,
            exact_match=exact_match,
            include_hidden=include_hidden,
            include_config=include_config,
            group_by_domain=group_by_domain,
            per_domain_limit=per_domain_limit,
            state_filter=state_filter,
            result_fields=result_fields,
            fields=fields,
            config_time_budget=config_time_budget,
        )
