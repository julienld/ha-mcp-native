"""Native recorder service; Core owns queries, schemas, metadata and conversion."""

import copy
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from homeassistant.core import HomeAssistant

from ha_mcp.errors import create_validation_error
from ha_mcp.runtime import history_policy as policy
from ha_mcp.runtime.core_options import merge_core_options
from ha_mcp.runtime.errors import raise_failure
from ha_mcp.runtime.history_results import history_result, statistics_result
from ha_mcp.runtime.progress import report_progress
from ha_mcp.runtime.projection import (
    project_fields,
    resolve_local_timezone,
    timezone_metadata,
)
from ha_mcp.runtime.statistics_resets import restore_reset_timestamps
from ha_mcp.runtime.statistics_results import (
    _parse_statistic_types,
    apply_requested_units,
    format_entity_statistics,
)
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.tools.coercion import parse_string_list_param
from ha_mcp.tools.history_response import format_history_response
from ha_mcp.utils.owned_executor import await_owned_io

from ..core_contract import describe_contract, resolve_statistics_units
from .recorder_queries import RecorderQueries
from .registry_access import Authorize, authorize_actor, registry_failure

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class NativeHistory:
    hass: HomeAssistant
    actor: CallContext
    authorize: Authorize = field(kw_only=True, repr=False)
    guardrails: bool = field(default=False, kw_only=True)

    async def get(
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
        order: Literal["desc", "asc"],
        summary: bool,
        fields: str | list[str] | None,
        include_schema: bool,
        core_options: dict[str, Any] | None,
        ctx: Any = None,
    ) -> dict[str, Any]:
        try:
            await authorize_actor(self.actor, self.authorize)
            try:
                parsed_fields = (
                    parse_string_list_param(fields, "fields", allow_csv=True)
                    if fields is not None
                    else None
                )
            except ValueError as error:
                raise_failure(create_validation_error(str(error), parameter="fields"))
            entity_id_list = policy._parse_entity_ids(entity_ids)
            policy._validate_offset(
                offset,
                entity_count=len(entity_id_list),
                summary=summary and source == "history",
            )
            now = datetime.now(UTC)
            start, end = policy._parse_time_range(
                start_time, end_time, policy._DEFAULT_START_HOURS_BY_SOURCE[source], now
            )
            policy._validate_time_range(
                start,
                end,
                end_time_was_provided=end_time is not None,
                reject_zero_length=self.guardrails,
            )
            timezone_name = self.hass.config.time_zone
            timezone, resolved_name = resolve_local_timezone(timezone_name)
            if (
                self.guardrails
                and source == "statistics"
                and period in policy._CALENDAR_STATISTICS_PERIODS
            ):
                policy._validate_calendar_statistics_range(start, end, period)
                if resolved_name != timezone_name:
                    raise ToolFailure(
                        "VALIDATION_INVALID_PARAMETER",
                        "Home Assistant reports a timezone that cannot be resolved for safe calendar statistics estimation",
                        status="not_started",
                    )
            policy._validate_query_workload(
                source=source,
                entity_ids=entity_id_list,
                start_dt=start,
                end_dt=end,
                minimal_response=minimal_response,
                significant_changes_only=significant_changes_only,
                period=period,
                enforce_budget=self.guardrails,
                statistics_timezone=timezone,
            )
            await report_progress(
                self.actor.progress,
                progress=0,
                total=3,
                message="preparing recorder query",
            )
            reader = RecorderQueries(self)
            await report_progress(
                self.actor.progress,
                progress=1,
                total=3,
                message=f"querying recorder ({source})",
            )
            if source == "statistics":
                inner = await self._statistics(
                    reader,
                    entity_id_list,
                    start,
                    end,
                    period,
                    statistic_types,
                    limit,
                    offset,
                    core_options,
                )
                command = "recorder/statistics_during_period"
            else:
                command = "history/history_during_period"
                params = merge_core_options(
                    {
                        "start_time": start.isoformat(),
                        "end_time": end.isoformat(),
                        "entity_ids": entity_id_list,
                        "minimal_response": minimal_response,
                        "significant_changes_only": significant_changes_only,
                        "no_attributes": minimal_response,
                    },
                    core_options,
                )
                data = await reader.history(params)
                effective_limit = (
                    min(limit, policy._MAX_HISTORY_LIMIT)
                    if limit is not None
                    else policy._DEFAULT_HISTORY_LIMIT
                )
                inner = await await_owned_io(
                    self.hass.async_add_executor_job(
                        history_result,
                        data,
                        entity_id_list,
                        start,
                        end,
                        minimal_response,
                        significant_changes_only,
                        effective_limit,
                        offset or 0,
                        order,
                        min(end, now) if summary else None,
                    )
                )
            if include_schema:
                self._include_schema(inner, command)

            def project() -> dict[str, Any]:
                result = format_history_response(
                    timezone_metadata(
                        inner, timezone_name, False, convert_timestamps=False
                    )
                )
                result["data"] = project_fields(
                    result["data"],
                    parsed_fields,
                    extra_always_keep=frozenset({"core_contract"})
                    if include_schema
                    else None,
                )
                if warnings := result["data"].pop("warnings", None):
                    result.setdefault("warnings", []).extend(warnings)
                return result

            result: dict[str, Any] = await await_owned_io(
                self.hass.async_add_executor_job(project)
            )
            await report_progress(
                self.actor.progress,
                progress=3,
                total=3,
                message="recorder query complete",
            )
            await authorize_actor(self.actor, self.authorize)
            return result
        except Exception as error:
            raise registry_failure(error, {"source": source}) from error

    def _include_schema(self, inner: dict[str, Any], command: str) -> None:
        try:
            contract = describe_contract(self.hass, command)
        except Exception:
            _LOGGER.warning("Optional Core schema discovery failed", exc_info=True)
            contract = {
                "status": "unavailable",
                "reason": "Core schema discovery failed; the read result is preserved.",
            }
        warnings = contract.pop("warnings", [])
        if contract.get("status") == "unavailable":
            warnings.append(contract["reason"])
        if warnings:
            inner.setdefault("warnings", []).extend(warnings)
        inner["core_contract"] = contract

    async def _statistics(
        self,
        reader: RecorderQueries,
        entity_ids: list[str],
        start: datetime,
        end: datetime,
        period: str,
        statistic_types: str | list[str] | None,
        limit: int | None,
        offset: int | None,
        core_options: dict[str, Any] | None,
    ) -> dict[str, Any]:
        types = _parse_statistic_types(statistic_types)
        params = merge_core_options(
            {
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "statistic_ids": entity_ids,
                "period": period,
                "types": types,
            },
            core_options,
        )
        if types is None:
            params.pop("types")
        rows = await reader.statistics(params)
        metadata, failure, resolved = await self._metadata(
            entity_ids, params.get("units", {})
        )
        effective_limit = limit if limit is not None else policy._DEFAULT_HISTORY_LIMIT
        entities = format_entity_statistics(
            rows, entity_ids, period, offset or 0, effective_limit, metadata, failure
        )
        if params.get("units"):
            apply_requested_units(entities, resolved, params["units"])
        reset_warnings = await restore_reset_timestamps(
            reader.statistics, rows, metadata, params, resolved
        )
        result: dict[str, Any] = await await_owned_io(
            self.hass.async_add_executor_job(
                statistics_result,
                rows,
                entities,
                start,
                end,
                period,
                types,
                effective_limit,
                offset or 0,
                reset_warnings,
            )
        )
        return result

    async def _metadata(
        self, entity_ids: list[str], units: dict[str, Any]
    ) -> tuple[dict[str, Any], str | None, dict[str, Any]]:
        from homeassistant.components.recorder.statistics import (
            async_list_statistic_ids,
        )
        from homeassistant.helpers.json import json_bytes

        await authorize_actor(self.actor, self.authorize)
        metadata, resolved, failure = {}, {}, None
        try:
            records = await await_owned_io(
                async_list_statistic_ids(self.hass, set(entity_ids))
            )
            records = json.loads(json_bytes(records))
            metadata = {record["statistic_id"]: record for record in records}
        except Exception:
            _LOGGER.warning("Recorder statistics metadata lookup failed", exc_info=True)
            failure = "statistics_metadata_unavailable"
        await authorize_actor(self.actor, self.authorize)
        if metadata:
            try:
                resolved = {
                    record["statistic_id"]: record
                    for record in resolve_statistics_units(
                        copy.deepcopy(records), units
                    )
                }
            except Exception:
                _LOGGER.warning("Statistics converter resolution failed", exc_info=True)
        return metadata, failure, resolved
