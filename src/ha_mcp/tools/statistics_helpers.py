"""Recorder metadata and output units, using the running Core's display rules."""

import logging
from typing import Any

from ha_mcp.backends.legacy import legacy_errors_sync
from ha_mcp.runtime import statistics_results as _results

_parse_statistic_types = legacy_errors_sync(_results._parse_statistic_types)
format_entity_statistics = _results.format_entity_statistics
statistics_unit = _results.statistics_unit
statistics_warnings = _results.statistics_warnings

logger = logging.getLogger(__name__)


async def fetch_statistics_metadata(
    client: Any, statistic_ids: list[str]
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Read native metadata without making the values query depend on its availability."""
    if not statistic_ids:
        return {}, None
    try:
        response = await client.send_websocket_message(
            {"type": "recorder/get_statistics_metadata", "statistic_ids": statistic_ids}
        )
        if not response.get("success"):
            return {}, "statistics_metadata_unavailable"
        records = response.get("result")
        if not isinstance(records, list):
            return {}, "statistics_metadata_unavailable"
        return {
            record["statistic_id"]: record
            for record in records
            if isinstance(record, dict) and isinstance(record.get("statistic_id"), str)
        }, None
    except Exception:
        # Metadata is supplementary: preserve usable rows and explicitly leave
        # their units unresolved. Never silently substitute current state units.
        logger.warning("Recorder statistics metadata lookup failed", exc_info=True)
        return {}, "statistics_metadata_unavailable"


async def resolve_requested_units(
    client: Any, entities: list[dict[str, Any]], units: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Resolve explicit units and default converters absent from recorder metadata."""
    from ..client.websocket_client import get_websocket_client
    from .component_api import (
        component_supports,
        get_component_caps,
        invalidate_caps,
        is_unknown_command,
    )

    if not units:
        entities = [
            entity
            for entity in entities
            if (record := entity.get("statistics_metadata"))
            and record.get("unit_class") is None
            and record.get("display_unit_of_measurement")
            != record.get("statistics_unit_of_measurement")
        ]
    if not entities:
        return {}
    records: dict[str, dict[str, Any]] = {}
    try:
        caps = await get_component_caps(client)
        if component_supports(caps, "core_contract"):
            ws = await get_websocket_client(
                url=client.base_url,
                token=client.token,
                verify_ssl=getattr(client, "verify_ssl", None),
            )
            response = await ws.send_command(
                "ha_mcp_tools/statistics_units",
                statistic_ids=[e["entity_id"] for e in entities],
                units=units,
            )
            records = {r["statistic_id"]: r for r in response["result"]["records"]}
    except Exception as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        logger.warning("Statistics converter resolution failed", exc_info=True)
    if not units:
        # Default labels already come from recorder metadata; this lookup only
        # supplies converter classes needed to recover reset timestamps.
        return records
    _results.apply_requested_units(entities, records, units)
    return records


__all__ = [
    "_parse_statistic_types",
    "fetch_statistics_metadata",
    "resolve_requested_units",
]
