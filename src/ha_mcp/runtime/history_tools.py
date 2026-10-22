"""Public recorder contract shared by the Core service and legacy compatibility."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from pydantic import Field

from ha_mcp.tools.coercion import JSON_STRING_COERCION


class HistoryFacade:
    def __init__(self, get_history: Callable[..., Awaitable[dict[str, Any]]]) -> None:
        self.get_history = get_history

    async def ha_get_history(
        self,
        entity_ids: Annotated[
            str | list[str],
            JSON_STRING_COERCION,
            Field(
                description="Entity ID(s) to query. Can be a single ID, comma-separated string, or JSON array."
            ),
        ],
        source: Annotated[
            Literal["history", "statistics"],
            Field(
                description=(
                    'Data source: "history" for raw state changes at full resolution (~10 '
                    'day retention), or "statistics" for pre-aggregated long-term data '
                    "(permanent, requires state_class)."
                ),
                default="history",
            ),
        ] = "history",
        start_time: Annotated[
            str | None,
            Field(
                description="Start time: ISO datetime or a look-back from now with unit min, h, d, w or m (= 30 days), e.g. '30min' = 30 minutes ago. Default: 24h ago for history, 30d ago for statistics",
                default=None,
            ),
        ] = None,
        end_time: Annotated[
            str | None,
            Field(
                description="End time: same formats as start_time. Omit it to end at the current time",
                default=None,
            ),
        ] = None,
        # History-specific (ignored when source="statistics")
        minimal_response: Annotated[
            bool,
            Field(
                description="Return only states/timestamps without attributes. Ignored when "
                'source="statistics"',
                default=True,
            ),
        ] = True,
        significant_changes_only: Annotated[
            bool,
            Field(
                description="Filter to significant state changes only. Ignored when "
                'source="statistics"',
                default=True,
            ),
        ] = True,
        limit: Annotated[
            int | None,
            Field(
                description='Max entries per entity. Default: 100. For source="history": state '
                "rows; with summary=True, the states listed in summary.states, longest "
                'first. For source="statistics": aggregated rows. With multiple '
                "entity_ids, total rows returned can reach limit × len(entity_ids).",
                default=None,
                ge=1,
                le=1000,
            ),
        ] = None,
        offset: Annotated[
            int | None,
            Field(
                description="Number of entries to skip per entity for pagination.",
                default=None,
                ge=0,
            ),
        ] = None,
        # Statistics-specific (ignored when source="history")
        period: Annotated[
            str,
            Field(
                description='Aggregation period: "5minute", "hour", "day", "week", "month", "year".'
                ' Ignored when source="history"',
                default="day",
            ),
        ] = "day",
        statistic_types: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description='Native Core statistics types (for example "sum", "change", "last_reset"). Default: Core chooses all. Use include_schema=True to discover the installed contract. Ignored when source="history"',
                default=None,
            ),
        ] = None,
        order: Annotated[
            Literal["asc", "desc"],
            Field(
                default="desc",
                description=(
                    'Sort order for history entries. "desc": newest first. "asc": oldest '
                    'first. Ignored when source="statistics".'
                ),
            ),
        ] = "desc",
        summary: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Return per-entity totals instead of state rows: changes "
                    "(switches to a different state; the state at the window start is "
                    "not one, but without that state, e.g. with core_options "
                    "{'include_start_time_state': false} or purged history, the first "
                    "recorded state counts as one), first_change and last_change (null "
                    "without changes), and per state its periods (separate spells) and "
                    "duration_s (seconds, up to end_time or now, whichever is "
                    "earlier). distinct_states counts every state, listed or not. "
                    'Ignored when source="statistics".'
                ),
            ),
        ] = False,
        fields: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "Return only the specified keys within data to reduce response "
                    "size. None = full response. History keys: success, source, entities, "
                    "period, query_params. Statistics keys: success, source, entities, "
                    "period_type, time_range, statistic_types, query_params. Warnings and timezone metadata remain outside data and are always retained."
                ),
            ),
        ] = None,
        include_schema: Annotated[
            bool,
            Field(
                description="Include the running Core's native request schema when the component supports discovery."
            ),
        ] = False,
        core_options: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description="Additional native Core request fields, e.g. {'units': {'energy': 'MWh'}} for statistics. Core validates them. Cannot override fields controlled by the tool's parameters or query limits."
            ),
        ] = None,
        ctx: Any = None,
    ) -> dict[str, Any]:
        """Get historical data from Home Assistant's recorder.

        WHEN NOT TO USE:
        For current values, use ha_get_state. To inspect Energy Dashboard
        preferences and configured statistics, use
        ha_manage_energy_prefs(mode="get", include_statistics=True).

        WHEN TO USE:
        Use source="history" (default) to troubleshoot why a value changed, check
        event sequences, or analyze recent patterns. Use summary=True when the
        question needs a count or a duration ("was it ever unavailable?") rather
        than the rows. Use source="statistics" for long-term trends and period
        averages.

        CAVEATS:
        History rows rename Core's compact keys once: s -> state, a -> attributes,
        lu -> last_updated, lc -> last_changed. History event times are ISO strings
        in HA's timezone (UTC if unavailable). When Core omits lc, last_changed
        falls back to last_updated; this may not be the actual state-change time
        with significant_changes_only (default) or a window-start snapshot.
        Attributes and unknown fields pass through unchanged.
        Statistics timestamps use Unix milliseconds. Minimal history may omit attributes.
        No duplicate compact aliases are added. include_schema=True
        retains core_contract even when fields selects other data keys.

        Output units come from
        Core recorder metadata and reflect its display-unit conversion; unresolved
        units include a reason. statistics_metadata preserves Core's native fields.

        All data is fetched from HA before slicing; limit/offset are client-side.
        With multiple entity_ids, offset must be 0 — use a single entity_id for
        offset > 0. Use has_more and next_offset from the response to paginate.
        With source="history", summary=True does not combine with offset > 0.
        Administrators can optionally enable recorder workload guardrails in
        Advanced settings. When enabled, oversized entity/time workloads are
        rejected before the recorder query is issued; narrow the time range or
        entity list to stay within the budget. Calendar statistics may first
        read HA's configured timezone so the estimate follows local calendar
        boundaries.

        EXAMPLES:
        - ha_get_history(entity_ids="sensor.bedroom_temperature", start_time="24h")
        - Paginate oldest-first: ha_get_history(entity_ids="sensor.temperature", start_time="7d", limit=100, offset=100, order="asc")
        - Time per state: ha_get_history(entity_ids="binary_sensor.door", start_time="7d", summary=True)
        - ha_get_history(source="statistics", entity_ids="sensor.living_room_temperature", start_time="6m", period="month", statistic_types=["mean", "min", "max"])
        """
        return await self.get_history(
            entity_ids,
            source,
            start_time,
            end_time,
            minimal_response,
            significant_changes_only,
            limit,
            offset,
            period,
            statistic_types,
            order,
            summary,
            fields,
            include_schema,
            core_options,
            ctx,
        )
