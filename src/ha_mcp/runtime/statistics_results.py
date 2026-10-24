"""Pure recorder metadata, units and result projection."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tools.coercion import parse_string_list_param

from .errors import raise_failure
from .projection import build_pagination_metadata


def statistics_unit(
    metadata: dict[str, Any] | None, failure: str | None = None
) -> dict[str, Any]:
    """Label default statistics output with Core's resolved display unit.

    Core's statistics_during_period converts stored values to display units
    when no explicit units are requested. get_statistics_metadata calls Core's
    get_display_unit, including its handling of missing entities and invalid
    state units. Stored units alone cannot establish the output unit.
    """
    unknown = {"unit_of_measurement": None, "unit_source": "unknown"}
    if failure or metadata is None:
        return {**unknown, "unit_reason": failure or "statistics_metadata_missing"}
    if "display_unit_of_measurement" not in metadata:
        return {**unknown, "unit_reason": "display_unit_not_reported"}
    unit = metadata["display_unit_of_measurement"]
    if unit is not None and not isinstance(unit, str):
        return {**unknown, "unit_reason": "invalid_display_unit_metadata"}
    result = {"unit_of_measurement": unit, "unit_source": "recorder_metadata"}
    if unit is None:
        result["unit_reason"] = "statistics_are_unitless"
    return result


def format_entity_statistics(
    result_data: dict[str, Any],
    entity_ids: list[str],
    period: str,
    offset: int,
    limit: int,
    metadata: dict[str, dict[str, Any]],
    metadata_failure: str | None,
) -> list[dict[str, Any]]:
    """Preserve Core's row fields and attach units independently of pagination."""
    entities = []
    for entity_id in entity_ids:
        rows = result_data.get(entity_id, [])
        page = rows[offset : offset + limit]
        record = metadata.get(entity_id)
        entities.append(
            {
                "entity_id": entity_id,
                "period": period,
                "statistics": page,
                **statistics_unit(record, metadata_failure),
                "statistics_metadata": record,
                **build_pagination_metadata(
                    total_count=len(rows), offset=offset, limit=limit, count=len(page)
                ),
            }
        )
    return entities


def statistics_warnings(entities: list[dict[str, Any]]) -> list[str]:
    """Make unresolved units visible to callers without discarding numeric data."""
    return [
        f"{entity['entity_id']}: statistics output unit is unknown "
        f"({entity['unit_reason']}); do not infer it from current state attributes."
        for entity in entities
        if entity["unit_source"] == "unknown"
    ]


def _parse_statistic_types(
    statistic_types: str | list[str] | None,
) -> list[str] | None:
    """Parse and validate the statistic_types param into a list (or None for all)."""
    stat_types_list: list[str] | None = None
    if statistic_types is not None:
        if isinstance(statistic_types, str):
            if statistic_types.startswith("["):
                stat_types_list = parse_string_list_param(
                    statistic_types, "statistic_types"
                )
            elif "," in statistic_types:
                stat_types_list = [
                    s.strip() for s in statistic_types.split(",") if s.strip()
                ]
            else:
                stat_types_list = [statistic_types.strip()]
        else:
            stat_types_list = list(statistic_types)
        if not stat_types_list:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "statistic_types cannot be empty: this tool requires at least one value field. Omit it for Core's defaults.",
                )
            )

    return stat_types_list


def apply_requested_units(
    entities: list[dict[str, Any]],
    records: dict[str, dict[str, Any]],
    units: dict[str, Any],
) -> None:
    for entity in entities:
        record = records.get(entity["entity_id"])
        if record is not None and "output_unit_of_measurement" in record:
            entity["unit_of_measurement"] = record["output_unit_of_measurement"]
            entity["unit_source"] = "core_converter"
            entity.pop("unit_reason", None)
        else:
            unit_class = (entity.get("statistics_metadata") or {}).get("unit_class")
            if isinstance(unit_class, str) and unit_class not in units:
                continue
            entity.update(
                unit_of_measurement=None,
                unit_source="unknown",
                unit_reason="requested_unit_resolution_unavailable",
            )
