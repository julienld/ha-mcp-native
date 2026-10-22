"""Recorder query bounds and input parsing; these limits are HA-MCP policy."""

import math
import re
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tools.coercion import parse_string_list_param
from ha_mcp.tools.statistics_calendar import (
    _CALENDAR_STATISTICS_PERIODS,
    _statistics_scan_window,
)

from .errors import raise_failure

_RELATIVE_TIME_UNIT_SECONDS = {
    "min": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
    "m": 30 * 24 * 60 * 60,
}


_RELATIVE_FORMAT_HINT = "Relative format: '30min', '24h', '7d', '2w', '1m' (= 30 days)"


_DEFAULT_START_HOURS_BY_SOURCE: dict[str, int] = {"history": 24, "statistics": 30 * 24}


_DEFAULT_HISTORY_LIMIT = 100


_MAX_HISTORY_LIMIT = 1000


_MAX_HISTORY_ENTITIES = 10


_MAX_HISTORY_ENTITY_HOURS = 240.0


_MAX_STATISTICS_ENTITIES = 25


_MAX_SHORT_TERM_STATISTICS_ROWS = 10_000


_MAX_LONG_TERM_STATISTICS_ROWS = 20_000


_VALID_STATISTICS_PERIODS = frozenset(
    {"5minute", "hour", *_CALENDAR_STATISTICS_PERIODS}
)


def parse_relative_time(
    time_str: str | None,
    default_hours: int = 24,
    *,
    reference_time: datetime | None = None,
) -> datetime:
    """Parse a time string that can be either ISO format or relative (e.g., '24h', '7d').

    Args:
        time_str: Time string in ISO format or relative format (e.g., "30min", "24h", "7d", "2w", "1m" (= 30 days))
        default_hours: Default hours to go back if time_str is None
        reference_time: Reference datetime for relative values and defaults.

    Returns:
        A datetime in the parsed value's timezone; relative values use ``reference_time``'s.
    """
    now = reference_time or datetime.now(UTC)
    if time_str is None:
        return now - timedelta(hours=default_hours)

    relative_pattern = r"^(\d+)(min|[hdwm])$"
    match = re.match(relative_pattern, time_str.lower().strip())

    if match:
        value = int(match.group(1))
        unit = match.group(2)
        try:
            return now - timedelta(seconds=value * _RELATIVE_TIME_UNIT_SECONDS[unit])
        except OverflowError as exc:
            raise ValueError(
                f"Invalid time format: {time_str} is out of range"
            ) from exc

    try:
        if time_str.endswith("Z"):
            time_str = time_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(time_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, OverflowError) as e:
        raise ValueError(
            f"Invalid time format: {time_str}. Use ISO format or relative (e.g., '30min', '24h', '7d', '2w', '1m' (= 30 days))"
        ) from e


def _parse_entity_ids(entity_ids: str | list[str]) -> list[str]:
    """Parse entity_ids parameter into a list of strings."""
    if isinstance(entity_ids, str):
        if entity_ids.startswith("["):
            # Belt-and-suspenders: JSON_STRING_COERCION on the param already
            # parses a JSON-array string to a list upstream, so a string reaching
            # here is normally CSV/single. This branch stays as a fallback.
            parsed_ids = parse_string_list_param(entity_ids, "entity_ids")
            if parsed_ids is None:
                raise_failure(
                    create_error_response(
                        ErrorCode.VALIDATION_MISSING_PARAMETER,
                        "entity_ids is required",
                        suggestions=["Provide at least one entity ID"],
                    )
                )
            return parsed_ids
        elif "," in entity_ids:
            result = [e.strip() for e in entity_ids.split(",") if e.strip()]
            if not result:
                raise_failure(
                    create_error_response(
                        ErrorCode.VALIDATION_MISSING_PARAMETER,
                        "entity_ids is required",
                        suggestions=["Provide at least one entity ID"],
                    )
                )
            return result
        else:
            return [entity_ids.strip()]
    if not entity_ids:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_MISSING_PARAMETER,
                "entity_ids is required",
                suggestions=["Provide at least one entity ID"],
            )
        )

    return entity_ids


def _validate_offset(offset: int | None, *, entity_count: int, summary: bool) -> None:
    """Reject offsets the response could not honour."""
    if not offset:
        return
    # Summary first: dropping to one entity_id alone would still be rejected.
    if summary:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "summary=True covers the whole window and does not take an offset",
                context={"offset": offset},
                suggestions=[
                    "Omit offset, or set summary=False to page through state rows."
                ],
            )
        )
    # build_pagination_metadata applies per entity — limit=100 across
    # 5 entities returns up to 500 rows with no top-level has_more signal.
    if entity_count > 1:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "offset > 0 requires a single entity_id",
                context={"offset": offset, "entity_count": entity_count},
                suggestions=[
                    "Use a single entity_id when offset > 0, or use offset=0 for multi-entity requests."
                ],
            )
        )


def _parse_time_range(
    start_time: str | None,
    end_time: str | None,
    default_hours: int,
    reference_time: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Parse start_time and end_time into datetime objects."""
    reference_time = reference_time or datetime.now(UTC)
    try:
        start_dt = parse_relative_time(
            start_time,
            default_hours=default_hours,
            reference_time=reference_time,
        )
    except (ValueError, OverflowError) as e:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                str(e),
                context={"parameter": "start_time"},
                suggestions=[
                    "ISO format: '2025-01-25T00:00:00Z'",
                    _RELATIVE_FORMAT_HINT,
                ],
            )
        )

    if end_time:
        try:
            end_dt = parse_relative_time(
                end_time,
                default_hours=0,
                reference_time=reference_time,
            )
        except (ValueError, OverflowError) as e:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    str(e),
                    context={"parameter": "end_time"},
                    suggestions=[
                        "ISO format: '2025-01-26T00:00:00Z'",
                        _RELATIVE_FORMAT_HINT,
                    ],
                )
            )
    else:
        end_dt = reference_time

    return start_dt, end_dt


def _validate_time_range(
    start_dt: datetime,
    end_dt: datetime,
    *,
    end_time_was_provided: bool = True,
    reject_zero_length: bool = True,
) -> None:
    """Reject reversed ranges and, when requested, zero-length ranges."""
    if end_dt < start_dt or (reject_zero_length and end_dt == start_dt):
        if not end_time_was_provided and start_dt > end_dt:
            message = "start_time must not be in the future when end_time is omitted"
            suggestions = ["Choose a start_time at or before the current time."]
        else:
            message = "end_time must be later than start_time"
            suggestions = ["Choose an end_time later than start_time."]
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                message,
                context={
                    "start_time": start_dt.isoformat(),
                    "end_time": end_dt.isoformat(),
                },
                suggestions=suggestions,
            )
        )


def _validate_calendar_statistics_range(
    start_dt: datetime, end_dt: datetime, period: str
) -> None:
    """Reject boundary years that calendar alignment cannot advance safely."""
    if start_dt.year <= datetime.min.year or end_dt.year >= datetime.max.year:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Time range is outside the safe calendar alignment bounds for period='{period}'",
                context={
                    "start_time": start_dt.isoformat(),
                    "end_time": end_dt.isoformat(),
                    "period": period,
                },
                suggestions=[
                    "Choose start_time and end_time between years 2 and 9998."
                ],
            )
        )


def _validate_query_workload(
    *,
    source: str,
    entity_ids: list[str],
    start_dt: datetime,
    end_dt: datetime,
    minimal_response: bool,
    significant_changes_only: bool,
    period: str,
    enforce_budget: bool,
    statistics_timezone: tzinfo = UTC,
) -> None:
    """Reject recorder requests likely to monopolize Home Assistant resources."""
    if not enforce_budget:
        return

    _validate_time_range(start_dt, end_dt)
    if source == "history":
        violation = _history_workload_violation(
            entity_ids,
            start_dt,
            end_dt,
            minimal_response,
            significant_changes_only,
        )
    else:
        violation = _statistics_workload_violation(
            entity_ids, start_dt, end_dt, period, statistics_timezone
        )

    if violation is None:
        return
    context, suggestions = violation

    suggestions.append(
        "An administrator can disable history query guardrails in Advanced settings after evaluating the workload risk."
    )
    raise_failure(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            "Recorder query exceeds the safe workload budget",
            context=context,
            suggestions=suggestions,
        )
    )


def _history_workload_violation(
    entity_ids: list[str],
    start_dt: datetime,
    end_dt: datetime,
    minimal_response: bool,
    significant_changes_only: bool,
) -> tuple[dict[str, Any], list[str]] | None:
    """Return raw-history violation details, or None when within budget."""
    # Estimated multipliers account for the larger recorder payload when
    # filtering and attribute minimization are disabled; they are not measured
    # row counts.
    detail_weight = 1
    if not significant_changes_only:
        detail_weight *= 4
    if not minimal_response:
        detail_weight *= 2
    entity_count = len(entity_ids)
    estimated_entity_hours = (
        (end_dt - start_dt).total_seconds() / 3600 * entity_count * detail_weight
    )
    if (
        entity_count <= _MAX_HISTORY_ENTITIES
        and estimated_entity_hours <= _MAX_HISTORY_ENTITY_HOURS
    ):
        return None
    context = {
        "entity_count": entity_count,
        "max_entities": _MAX_HISTORY_ENTITIES,
        "estimated_entity_hours": round(estimated_entity_hours, 2),
        "max_entity_hours": _MAX_HISTORY_ENTITY_HOURS,
        "detail_weight": detail_weight,
        "minimal_response": minimal_response,
        "significant_changes_only": significant_changes_only,
    }
    suggestions = ["Query fewer entities or use a shorter time range."]
    if not significant_changes_only:
        suggestions.append("Set significant_changes_only=true to reduce recorder work.")
    if not minimal_response:
        suggestions.append("Set minimal_response=true to omit full state attributes.")
    suggestions.append(
        "Use source='statistics' for long ranges when the entities support long-term statistics."
    )
    return context, suggestions


def _statistics_workload_violation(
    entity_ids: list[str],
    start_dt: datetime,
    end_dt: datetime,
    period: str,
    statistics_timezone: tzinfo,
) -> tuple[dict[str, Any], list[str]] | None:
    """Return statistics violation details, or None when within budget."""
    if period not in _VALID_STATISTICS_PERIODS:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Cannot safely estimate recorder workload for period: {period}",
                context={"period": period},
                suggestions=[
                    "Use one of: '5minute', 'hour', 'day', 'week', 'month', 'year'."
                ],
            )
        )
    scan_start, scan_end = _statistics_scan_window(
        start_dt, end_dt, period, statistics_timezone
    )
    scan_granularity_seconds = 300 if period == "5minute" else 3600
    max_statistics_rows = (
        _MAX_SHORT_TERM_STATISTICS_ROWS
        if period == "5minute"
        else _MAX_LONG_TERM_STATISTICS_ROWS
    )
    entity_count = len(entity_ids)
    estimated_rows = (
        math.ceil((scan_end - scan_start).total_seconds() / scan_granularity_seconds)
        * entity_count
    )
    if (
        entity_count <= _MAX_STATISTICS_ENTITIES
        and estimated_rows <= max_statistics_rows
    ):
        return None
    context = {
        "entity_count": entity_count,
        "max_entities": _MAX_STATISTICS_ENTITIES,
        "estimated_rows": estimated_rows,
        "max_estimated_rows": max_statistics_rows,
        "period": period,
        "scan_granularity_minutes": scan_granularity_seconds // 60,
        "scan_start_time": scan_start.isoformat(),
        "scan_end_time": scan_end.isoformat(),
    }
    suggestions = ["Query fewer entities or use a shorter time range."]
    if period == "5minute":
        suggestions.append("Use period='hour' to scan the long-term statistics table.")
    return context, suggestions
