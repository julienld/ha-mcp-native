"""
Historical data access tools for Home Assistant MCP server.

This module provides tools for accessing historical data from Home Assistant's
recorder component via a single consolidated tool:

ha_get_history -- Retrieve historical data with source-selectable mode:
  - source="history" (default): Raw state changes, ~10 day retention
  - source="statistics": Pre-aggregated long-term statistics, permanent retention
"""

import logging
from datetime import UTC, datetime, tzinfo
from typing import Any, Literal

from ha_mcp._vendor.fastmcp import Context
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool
from ha_mcp.backends.legacy import legacy_errors, legacy_errors_sync
from ha_mcp.runtime import history_policy as _query_policy
from ha_mcp.runtime.history_results import history_result, statistics_result
from ha_mcp.runtime.history_tools import HistoryFacade

from ..config import get_global_settings
from ..errors import ErrorCode, create_error_response, create_validation_error
from .coercion import parse_string_list_param
from .core_contract import include_core_contract, merge_core_options
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    safe_progress,
)
from .history_response import (
    format_history_response,
)
from .recorder_errors import raise_recorder_ws_failure as _raise_recorder_ws_failure
from .response_helpers import (
    add_timezone_metadata,
    fetch_ha_timezone,
    project_fields,
    resolve_local_timezone,
)
from .statistics_calendar import (
    _CALENDAR_STATISTICS_PERIODS,
)
from .statistics_helpers import (
    _parse_statistic_types,
    fetch_statistics_metadata,
    format_entity_statistics,
    resolve_requested_units,
)
from .statistics_resets import restore_reset_timestamps
from .tool_hints import read_only_hints

_RELATIVE_TIME_UNIT_SECONDS = _query_policy._RELATIVE_TIME_UNIT_SECONDS
_DEFAULT_START_HOURS_BY_SOURCE = _query_policy._DEFAULT_START_HOURS_BY_SOURCE
_DEFAULT_HISTORY_LIMIT = _query_policy._DEFAULT_HISTORY_LIMIT
_MAX_HISTORY_LIMIT = _query_policy._MAX_HISTORY_LIMIT
parse_relative_time = _query_policy.parse_relative_time
_parse_entity_ids = legacy_errors_sync(_query_policy._parse_entity_ids)
_validate_offset = legacy_errors_sync(_query_policy._validate_offset)
_parse_time_range = legacy_errors_sync(_query_policy._parse_time_range)
_validate_time_range = legacy_errors_sync(_query_policy._validate_time_range)
_validate_calendar_statistics_range = legacy_errors_sync(
    _query_policy._validate_calendar_statistics_range
)
_validate_query_workload = legacy_errors_sync(_query_policy._validate_query_workload)

logger = logging.getLogger(__name__)


async def _get_statistics_timezone(client: Any) -> tzinfo:
    """Resolve HA's timezone without accepting an unsafe UTC fallback."""
    timezone_name, fetch_failed = await fetch_ha_timezone(client)
    if fetch_failed:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONNECTION_FAILED,
                "Could not fetch the Home Assistant timezone required for safe calendar statistics estimation",
                suggestions=[
                    "Check the Home Assistant connection, then retry.",
                    "Use period='hour' or period='5minute', which do not require calendar alignment.",
                ],
            )
        )
    timezone, resolved_timezone_name = resolve_local_timezone(timezone_name)
    if resolved_timezone_name != timezone_name:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Home Assistant reports a timezone that could not be resolved for safe calendar statistics estimation",
                context={"home_assistant_timezone": timezone_name},
                suggestions=[
                    "Configure a valid Home Assistant timezone, then retry.",
                    "Use period='hour' or period='5minute', which do not require calendar alignment.",
                ],
            )
        )
    return timezone


async def _prepare_guardrail_query(
    client: Any,
    *,
    enabled: bool,
    source: str,
    period: str,
    start_dt: datetime,
    end_dt: datetime,
    end_time_was_provided: bool,
) -> tzinfo:
    """Validate guarded ranges and resolve calendar statistics timezone."""
    _validate_time_range(
        start_dt,
        end_dt,
        end_time_was_provided=end_time_was_provided,
        reject_zero_length=enabled,
    )
    if not enabled:
        return UTC
    if source != "statistics" or period not in _CALENDAR_STATISTICS_PERIODS:
        return UTC
    _validate_calendar_statistics_range(start_dt, end_dt, period)
    return await _get_statistics_timezone(client)


class HistoryTools(HistoryFacade):
    """Historical data access tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client
        super().__init__(self._get_history)

    ha_get_history = tool(
        name="ha_get_history",
        tags={"History & Statistics"},
        annotations=read_only_hints(
            "Get Entity History or Statistics", open_world=False
        ),
    )(log_tool_usage(legacy_errors(HistoryFacade.ha_get_history)))

    async def _get_history(
        self,
        entity_ids: str | list[str],
        source: Literal["history", "statistics"],
        start_time: str | None,
        end_time: str | None,
        minimal_response: bool,
        significant_changes_only: bool,
        limit: int | None,
        offset: int | None,
        period: str,
        statistic_types: str | list[str] | None,
        order: Literal["asc", "desc"],
        summary: bool,
        fields: str | list[str] | None,
        include_schema: bool,
        core_options: dict[str, Any] | None,
        ctx: Context | None,
    ) -> dict[str, Any]:
        parsed_fields: list[str] | None = None
        if fields is not None:
            try:
                parsed_fields = parse_string_list_param(
                    fields, "fields", allow_csv=True
                )
            except ValueError as exc:
                raise_tool_error(create_validation_error(str(exc), parameter="fields"))
        try:
            entity_id_list = _parse_entity_ids(entity_ids)
            _validate_offset(
                offset,
                entity_count=len(entity_id_list),
                summary=summary and source == "history",
            )

            default_hours = _DEFAULT_START_HOURS_BY_SOURCE[source]
            now = datetime.now(UTC)
            start_dt, end_dt = _parse_time_range(
                start_time, end_time, default_hours, now
            )
            query_settings = get_global_settings()
            statistics_timezone = await _prepare_guardrail_query(
                self._client,
                enabled=query_settings.enable_history_query_guardrails,
                source=source,
                period=period,
                start_dt=start_dt,
                end_dt=end_dt,
                end_time_was_provided=end_time is not None,
            )
            _validate_query_workload(
                source=source,
                entity_ids=entity_id_list,
                start_dt=start_dt,
                end_dt=end_dt,
                minimal_response=minimal_response,
                significant_changes_only=significant_changes_only,
                period=period,
                enforce_budget=query_settings.enable_history_query_guardrails,
                statistics_timezone=statistics_timezone,
            )

            logger.debug(
                f"ha_get_history starting: source={source} "
                f"entities={len(entity_id_list)} "
                f"window={start_dt.isoformat()}..{end_dt.isoformat()}",
            )
            await safe_progress(
                ctx,
                progress=0,
                total=3,
                message="connecting to Home Assistant WebSocket",
            )

            await safe_progress(
                ctx,
                progress=1,
                total=3,
                message=f"querying recorder ({source})",
            )

            # Route through the shared pooled WebSocket (issue #1813) instead of
            # a dedicated connect/auth handshake per call. Statistics also fetches
            # native recorder metadata through the same pooled client.
            if source == "statistics":
                inner = await _fetch_statistics(
                    self._client,
                    entity_id_list,
                    start_dt,
                    end_dt,
                    period,
                    statistic_types,
                    limit,
                    offset,
                    core_options=core_options,
                )
            else:
                inner = await _fetch_history(
                    self._client,
                    entity_id_list,
                    start_dt,
                    end_dt,
                    minimal_response,
                    significant_changes_only,
                    limit,
                    offset,
                    _DEFAULT_HISTORY_LIMIT,
                    _MAX_HISTORY_LIMIT,
                    order=order,
                    core_options=core_options,
                    summary_until=min(end_dt, now) if summary else None,
                )
            if include_schema:
                command = (
                    "recorder/statistics_during_period"
                    if source == "statistics"
                    else "history/history_during_period"
                )
                await include_core_contract(self._client, inner, command)
            await safe_progress(
                ctx,
                progress=3,
                total=3,
                message="recorder query complete",
            )
            # Wrap first so the outer {"data": ..., "metadata": ...} shape
            # is always present; then project the inner data dict in-place
            # when caller requested field projection.
            _r = await add_timezone_metadata(
                self._client, inner, convert_timestamps=False
            )
            _r = format_history_response(_r)
            _r["data"] = project_fields(
                _r["data"],
                parsed_fields,
                extra_always_keep=frozenset({"core_contract"})
                if include_schema
                else None,
            )
            if warnings := _r["data"].pop("warnings", None):
                _r.setdefault("warnings", []).extend(warnings)
            return _r

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            if source == "statistics":
                suggestions = [
                    "Check Home Assistant connection",
                    "Verify entities have state_class attribute",
                    "Ensure recorder component is enabled with statistics",
                ]
            else:
                suggestions = [
                    "Check Home Assistant connection",
                    "Verify entity IDs are correct",
                    "Ensure recorder component is enabled",
                ]
            exception_to_structured_error(e, suggestions=suggestions)
            return (
                None  # exception_to_structured_error always raises; explicit for CodeQL
            )


def register_history_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register historical data access tools with the MCP server."""
    register_tool_methods(mcp, HistoryTools(client))


async def _fetch_history(
    client: Any,
    entity_id_list: list[str],
    start_dt: datetime,
    end_dt: datetime,
    minimal_response: bool,
    significant_changes_only: bool,
    limit: int | None,
    offset: int | None,
    default_limit: int,
    max_limit: int,
    order: str = "desc",
    core_options: dict[str, Any] | None = None,
    summary_until: datetime | None = None,
) -> dict[str, Any]:
    """Execute the history/history_during_period WebSocket call.

    *order* controls state-list ordering: ``"desc"`` (default) returns the
    newest states first; ``"asc"`` returns the oldest first. With
    *summary_until*, each entity carries ``summarize_states`` totals up to that
    time instead of its rows.

    Returns the unwrapped history dict; the caller is responsible for projection
    and wrapping with ``add_timezone_metadata``.
    """
    effective_limit = min(limit, max_limit) if limit is not None else default_limit
    effective_offset = offset if offset is not None else 0

    command_params = {
        "start_time": start_dt.isoformat(),
        "end_time": end_dt.isoformat(),
        "entity_ids": entity_id_list,
        "minimal_response": minimal_response,
        "significant_changes_only": significant_changes_only,
        "no_attributes": minimal_response,
    }

    command_params = merge_core_options(command_params, core_options)
    response = await client.send_websocket_message(
        {"type": "history/history_during_period", **command_params}
    )

    if not response.get("success"):
        _raise_recorder_ws_failure(
            "history",
            response.get("error", "Unknown error"),
            entity_id_list,
            error_code=response.get("error_code"),
            suggestions=[
                "Verify entity IDs exist using ha_search()",
                "Check that entities are recorded (not excluded from recorder)",
                "Ensure time range is within recorder retention period (~10 days)",
            ],
        )

    result_data = response.get("result", {})
    return history_result(
        result_data,
        entity_id_list,
        start_dt,
        end_dt,
        minimal_response,
        significant_changes_only,
        effective_limit,
        effective_offset,
        order,
        summary_until,
    )


async def _fetch_statistics(
    client: Any,
    entity_id_list: list[str],
    start_dt: datetime,
    end_dt: datetime,
    period: str,
    statistic_types: str | list[str] | None,
    limit: int | None,
    offset: int | None,
    core_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute the recorder/statistics_during_period WebSocket call.

    Returns the unwrapped statistics dict; the caller is responsible for projection
    and wrapping with ``add_timezone_metadata``.
    """
    effective_limit = limit if limit is not None else _DEFAULT_HISTORY_LIMIT
    effective_offset = offset if offset is not None else 0

    stat_types_list = _parse_statistic_types(statistic_types)

    command_params: dict[str, Any] = {
        "start_time": start_dt.isoformat(),
        "end_time": end_dt.isoformat(),
        "statistic_ids": entity_id_list,
        "period": period,
    }
    command_params = merge_core_options(
        {**command_params, "types": stat_types_list}, core_options
    )
    if stat_types_list is None:
        command_params.pop("types")

    response = await client.send_websocket_message(
        {"type": "recorder/statistics_during_period", **command_params}
    )

    if not response.get("success"):
        _raise_recorder_ws_failure(
            "statistics",
            response.get("error", "Unknown error"),
            entity_id_list,
            error_code=response.get("error_code"),
            suggestions=[
                "Verify entities have state_class attribute (measurement, total, total_increasing)",
                "Use ha_search() to check entity attributes",
                "Statistics are only available for entities that track numeric values",
            ],
        )

    result_data = response.get("result", {})
    metadata, metadata_failure = await fetch_statistics_metadata(client, entity_id_list)
    entities_statistics = format_entity_statistics(
        result_data,
        entity_id_list,
        period,
        effective_offset,
        effective_limit,
        metadata,
        metadata_failure,
    )
    resolved_units = await resolve_requested_units(
        client, entities_statistics, command_params.get("units", {})
    )
    reset_warnings = await restore_reset_timestamps(
        client, result_data, metadata, command_params, resolved_units
    )
    return statistics_result(
        result_data,
        entities_statistics,
        start_dt,
        end_dt,
        period,
        stat_types_list,
        effective_limit,
        effective_offset,
        reset_warnings,
    )


__all__ = [
    "HistoryTools",
    "_RELATIVE_TIME_UNIT_SECONDS",
    "parse_relative_time",
    "register_history_tools",
]
