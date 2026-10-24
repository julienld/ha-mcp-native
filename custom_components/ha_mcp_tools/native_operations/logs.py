"""Canonical log sources backed by Core collections, recorder and configured files."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from homeassistant.core import HomeAssistant

from ha_mcp.runtime.error_log_parsing import _error_log_window_lines
from ha_mcp.runtime.fault_log_results import (
    FAULT_LOG_PATH,
    FAULT_LOG_WINDOW_LINES,
    _no_crash,
    _shape_crash_page,
)
from ha_mcp.runtime.log_common import (
    DEFAULT_LOG_LIMIT,
    SUPERVISOR_SEARCH_WINDOW_LINES,
    _coerce_limit,
)
from ha_mcp.runtime.log_query import LogQuery
from ha_mcp.runtime.log_results import LogResults, supervisor_log_result
from ha_mcp.runtime.log_window import MIN_LOG_WINDOW_LINES
from ha_mcp.runtime.logbook_results import LogbookResults
from ha_mcp.runtime.projection import timezone_metadata
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation
from ha_mcp.utils.owned_executor import await_owned_io

from .log_files import NativeLogFiles
from .registry_access import Authorize, authorize_actor, registry_failure
from .registry_failures import with_recovery_hint


@dataclass(slots=True)
class NativeLogs(LogQuery, LogResults, LogbookResults):
    hass: HomeAssistant
    actor: CallContext
    authorize: Authorize = field(kw_only=True, repr=False)

    async def get(self, **arguments: Any) -> dict[str, Any]:
        try:
            await authorize_actor(self.actor, self.authorize)
            result = await LogQuery.get_logs(self, **arguments)
            await authorize_actor(self.actor, self.authorize)
            return result
        except Exception as error:
            raise registry_failure(
                error, {"source": arguments.get("source")}
            ) from error

    async def _get_system_log(
        self,
        limit: int | None,
        search: str | None,
        level: str | None,
        order: str,
        compact: bool,
    ) -> dict[str, Any]:
        from homeassistant.components.system_log import DOMAIN
        from homeassistant.helpers.json import json_bytes

        handler = self.hass.data.get(DOMAIN)
        if handler is None:
            raise UnsupportedOperation("Core system_log is not loaded")
        # The Core producer returns new entry dictionaries and message lists.
        # Keep that callback on its owning loop; shape only detached rows off-loop.
        rows = handler.records.to_list()

        def project() -> dict[str, Any]:
            return self.system_log_result(
                json.loads(json_bytes(rows)),
                _coerce_limit(limit),
                search,
                level,
                order,
                compact,
            )

        result: dict[str, Any] = await await_owned_io(
            self.hass.async_add_executor_job(project)
        )
        return result

    async def _get_logger_info(
        self, limit: int | None, search: str | None
    ) -> dict[str, Any]:
        from .integration_inspection import logger_levels

        rows = logger_levels(self.hass)
        if rows is None:
            raise UnsupportedOperation("Core logger is not loaded")
        result: dict[str, Any] = await await_owned_io(
            self.hass.async_add_executor_job(
                self.logger_result, rows, _coerce_limit(limit), search
            )
        )
        return result

    async def _get_error_log(
        self,
        limit: int | None,
        search: str | None,
        level: str | None,
        order: Literal["newest", "oldest"],
        offset: int,
        structured: bool,
        top_n: int | None,
    ) -> dict[str, Any]:
        lines = _error_log_window_lines(limit, search, level, structured)
        page = await await_owned_io(NativeLogFiles(self).error_log(lines, offset))
        result: dict[str, Any] = await await_owned_io(
            self.hass.async_add_executor_job(
                self.error_log_result,
                page,
                lines,
                limit,
                search,
                level,
                order,
                offset,
                structured,
                top_n,
            )
        )
        return result

    async def _get_supervisor_log(
        self,
        slug: str,
        limit: int | None,
        search: str | None,
        order: Literal["newest", "oldest"],
    ) -> dict[str, Any]:
        return await self._supervisor_source("supervisor", slug, limit, search, order)

    async def _get_system_service_log(
        self,
        service: str,
        limit: int | None,
        search: str | None,
        order: Literal["newest", "oldest"],
    ) -> dict[str, Any]:
        return await self._supervisor_source(
            "system_service", service, limit, search, order
        )

    async def _supervisor_source(
        self,
        source: str,
        slug: str,
        limit: int | None,
        search: str | None,
        order: Literal["newest", "oldest"],
    ) -> dict[str, Any]:
        effective = _coerce_limit(
            limit, default=DEFAULT_LOG_LIMIT, suggestion_example="100"
        )
        lines = max(effective, SUPERVISOR_SEARCH_WINDOW_LINES) if search else effective
        try:
            text = await await_owned_io(
                NativeLogFiles(self).supervisor(
                    slug if source == "system_service" else None,
                    slug if source == "supervisor" else None,
                    max(lines, MIN_LOG_WINDOW_LINES),
                )
            )
        except ToolFailure as error:
            error.data.update(source=source, slug=slug)
            with_recovery_hint(
                error,
                "Use ha_get_app() to check installed app slugs and verify Supervisor is available on this installation"
                if source == "supervisor"
                else "Verify Supervisor is available and use a supported system service slug",
            )
            raise
        result: dict[str, Any] = await await_owned_io(
            self.hass.async_add_executor_job(
                supervisor_log_result, text, source, slug, effective, search, order
            )
        )
        return result

    async def _get_fault_log(
        self,
        limit: int | None,
        search: str | None,
        offset: int,
        order: Literal["newest", "oldest"],
    ) -> dict[str, Any]:
        effective = _coerce_limit(
            limit, default=DEFAULT_LOG_LIMIT, suggestion_example="100"
        )
        captured = await NativeLogFiles(self).fault_log()

        def project() -> dict[str, Any]:
            data: dict[str, Any] = {
                "success": True,
                "source": "fault_log",
                "path": FAULT_LOG_PATH,
                "limit": effective,
                "offset": offset,
                "order": order,
                "window_lines": FAULT_LOG_WINDOW_LINES,
            }
            lines = captured["content"].splitlines()
            if captured.get("mtime") is not None:
                data["modified"] = datetime.fromtimestamp(
                    captured["mtime"], UTC
                ).isoformat()
            if not any(line.strip() for line in lines):
                return _no_crash(data, total_lines=captured["total_lines"])
            data.update(
                total_lines=captured["total_lines"],
                window_truncated=captured["truncated"],
            )
            return _shape_crash_page(
                data, lines, limit=effective, offset=offset, search=search, order=order
            )

        result: dict[str, Any] = await await_owned_io(
            self.hass.async_add_executor_job(project)
        )
        return result

    async def _get_logbook(
        self,
        hours_back: int,
        entity_id: str | None,
        device_id: str | None,
        context_id: str | None,
        end_time: str | None,
        limit: int | None,
        offset: int,
        search: str | None,
        compact: bool,
        order: Literal["newest", "oldest"],
    ) -> dict[str, Any]:
        from homeassistant.components.recorder import get_instance
        from homeassistant.helpers.json import json_bytes

        from ..websocket_api.logbook import prepare_logbook_query

        hours, effective, skip = self._coerce_logbook_params(hours_back, limit, offset)
        if context_id and (entity_id or device_id):
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                "context_id cannot be combined with entity_id or device_id",
                status="not_started",
            )
        end = (
            datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            if end_time
            else datetime.now(UTC)
        )
        start = end - timedelta(hours=hours)
        query_start, query_end, processor = prepare_logbook_query(
            self.hass,
            start.isoformat(),
            end.isoformat(),
            self._split_logbook_filter(entity_id) or None,
            self._split_logbook_filter(device_id) or None,
            context_id,
        )
        timezone = self.hass.config.time_zone

        def read() -> dict[str, Any]:
            rows = (
                json.loads(json_bytes(processor.get_events(query_start, query_end)))
                if processor
                else []
            )
            return timezone_metadata(
                self.logbook_result(
                    rows,
                    hours,
                    effective,
                    skip,
                    start.isoformat(),
                    end,
                    entity_id,
                    device_id,
                    context_id,
                    search,
                    compact,
                    order,
                ),
                timezone,
                False,
            )

        await authorize_actor(self.actor, self.authorize)
        executor = get_instance(self.hass) if processor is not None else self.hass
        result: dict[str, Any] = await await_owned_io(
            executor.async_add_executor_job(read)
        )
        return result
