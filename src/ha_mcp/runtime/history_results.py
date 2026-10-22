"""Shared recorder result shaping, independent of the query transport."""

from datetime import datetime
from typing import Any

from ha_mcp.tools.history_response import summarize_states, summary_warnings

from .projection import build_pagination_metadata
from .statistics_results import statistics_warnings


def history_result(
    result_data: dict[str, Any],
    entity_id_list: list[str],
    start_dt: datetime,
    end_dt: datetime,
    minimal_response: bool,
    significant_changes_only: bool,
    effective_limit: int,
    effective_offset: int,
    order: str,
    summary_until: datetime | None,
) -> dict[str, Any]:
    if summary_until is not None:
        # ha_get_history passes min(end_time, the clock it read before this
        # query). Core answers a future end_time up to its own clock, which can
        # run past that reading. Like Core's end_time, summary_until is exclusive.
        until_ts = summary_until.timestamp()
        result_data = {
            entity_id: [row for row in rows if row["lu"] < until_ts]
            for entity_id, rows in result_data.items()
        }
    entities_history = []
    summaries: dict[str, dict[str, Any]] = {}
    start_ts = start_dt.timestamp()
    period = {"start": start_dt.isoformat(), "end": end_dt.isoformat()}

    for entity_id in entity_id_list:
        entity_states = result_data.get(entity_id, [])
        if summary_until is not None:
            summary = summaries[entity_id] = summarize_states(
                entity_states, start_ts, summary_until.timestamp(), effective_limit
            )
            entities_history.append(
                {"entity_id": entity_id, "period": period, "summary": summary}
            )
            continue
        if order == "desc":
            entity_states = list(reversed(entity_states))
        paged_states = entity_states[
            effective_offset : effective_offset + effective_limit
        ]

        pagination = build_pagination_metadata(
            total_count=len(entity_states),
            offset=effective_offset,
            limit=effective_limit,
            count=len(paged_states),
        )
        entities_history.append(
            {
                "entity_id": entity_id,
                "period": period,
                "states": paged_states,
                **pagination,
            }
        )

    history_data = {
        "success": True,
        "source": "history",
        "entities": entities_history,
        "period": period,
        "query_params": {
            "minimal_response": minimal_response,
            "significant_changes_only": significant_changes_only,
            "limit": effective_limit,
            "offset": effective_offset,
            "order": order,
            "summary": summary_until is not None,
        },
    }
    if summary_until is not None:  # ha_get_history drops an empty list
        history_data["warnings"] = summary_warnings(result_data, summaries, start_ts)

    return history_data


def statistics_result(
    result_data: dict[str, Any],
    entities_statistics: list[dict[str, Any]],
    start_dt: datetime,
    end_dt: datetime,
    period: str,
    stat_types_list: list[str] | None,
    effective_limit: int,
    effective_offset: int,
    reset_warnings: list[str],
) -> dict[str, Any]:
    all_stat_types = (
        stat_types_list
        if stat_types_list is not None
        else sorted(
            {
                key
                for rows in result_data.values()
                for row in rows
                for key in row
                if key not in {"start", "end"}
            }
        )
    )

    empty_entities: list[str] = [
        str(e["entity_id"]) for e in entities_statistics if e["count"] == 0
    ]

    statistics_data: dict[str, Any] = {
        "success": True,
        "source": "statistics",
        "entities": entities_statistics,
        "period_type": period,
        "time_range": {
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
        },
        "statistic_types": all_stat_types,
        "query_params": {
            "statistic_types": stat_types_list,
            "limit": effective_limit,
            "offset": effective_offset,
        },
    }

    warnings = statistics_warnings(entities_statistics) + reset_warnings
    if empty_entities:
        warnings += [
            f"No statistics found for: {', '.join(empty_entities)}. "
            "These entities may not have state_class attribute or may not have recorded data yet."
        ]

    if warnings:
        statistics_data["warnings"] = warnings
    return statistics_data
