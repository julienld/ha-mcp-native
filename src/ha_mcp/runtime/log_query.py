"""Cross-source log warnings and parameter rules, without a transport."""

from typing import Any, Literal, cast

from ha_mcp.tool_api import ToolFailure

from .error_log_journald import parse_cursor_token
from .log_common import _collect_log_warnings, _validate_log_level, _validate_log_slug


def _validate_log_offset(source: str, offset: int | str) -> int | str:
    """Validate a paging anchor: an integer, or an error-log cursor token."""
    if isinstance(offset, str):
        text = offset.strip()
        if not text:
            return 0
        try:
            offset = int(text)
        except ValueError:
            return _validate_cursor_offset(source, text)
    if offset < 0:
        raise ToolFailure(
            "VALIDATION_INVALID_PARAMETER",
            f"'offset' must not be negative, got {offset}",
            status="not_started",
        )
    return offset


def _validate_cursor_offset(source: str, offset: str) -> str:
    if source != "error_log":
        raise ToolFailure(
            "VALIDATION_INVALID_PARAMETER",
            "Cursor tokens only apply to source='error_log'; "
            f"source='{source}' takes a non-negative integer offset",
            status="not_started",
        )
    try:
        parse_cursor_token(offset)
    except ValueError as error:
        raise ToolFailure(
            "VALIDATION_INVALID_PARAMETER",
            f"Invalid 'offset' token for source='error_log' ({error}); pass the "
            "'next_offset' value a previous response returned",
            status="not_started",
        ) from error
    return offset


class LogQuery:
    _get_logbook: Any
    _get_system_log: Any
    _get_error_log: Any
    _get_logger_info: Any
    _get_fault_log: Any
    _get_system_service_log: Any
    _get_supervisor_log: Any

    async def _fetch_log_source(
        self,
        source: str,
        limit: int | None,
        search: str | None,
        hours_back: int,
        entity_id: str | None,
        device_id: str | None,
        context_id: str | None,
        end_time: str | None,
        offset: int | str,
        compact: bool,
        level: str | None,
        slug: str | None,
        order: Literal["newest", "oldest"],
        structured: bool = False,
        top_n: int | None = None,
    ) -> dict[str, Any]:
        numeric_offset = offset if isinstance(offset, int) else 0
        if source == "logbook":
            return cast(
                dict[str, Any],
                await self._get_logbook(
                    hours_back=hours_back,
                    entity_id=entity_id,
                    device_id=device_id,
                    context_id=context_id,
                    end_time=end_time,
                    limit=limit,
                    offset=numeric_offset,
                    search=search,
                    compact=compact,
                    order=order,
                ),
            )
        if source == "system":
            return cast(
                dict[str, Any],
                await self._get_system_log(
                    limit=limit,
                    search=search,
                    level=level,
                    order=order,
                    compact=compact,
                ),
            )
        if source == "error_log":
            return cast(
                dict[str, Any],
                await self._get_error_log(
                    limit=limit,
                    search=search,
                    level=level,
                    order=order,
                    offset=offset,
                    structured=structured,
                    top_n=top_n,
                ),
            )
        if source == "logger":
            # logger reports per-integration levels, not time-ordered events;
            # 'order' does not apply (a warning is emitted upstream).
            return cast(
                dict[str, Any], await self._get_logger_info(limit=limit, search=search)
            )
        if source == "fault_log":
            return cast(
                dict[str, Any],
                await self._get_fault_log(
                    limit=limit, search=search, offset=numeric_offset, order=order
                ),
            )
        if source == "system_service":
            assert slug is not None  # guaranteed by _validate_log_slug
            return cast(
                dict[str, Any],
                await self._get_system_service_log(
                    service=slug, limit=limit, search=search, order=order
                ),
            )
        assert slug is not None  # guaranteed by _validate_log_slug
        return cast(
            dict[str, Any],
            await self._get_supervisor_log(
                slug=slug, limit=limit, search=search, order=order
            ),
        )

    async def get_logs(
        self,
        source: str,
        limit: int | None,
        search: str | None,
        hours_back: int,
        entity_id: str | None,
        device_id: str | None,
        context_id: str | None,
        end_time: str | None,
        offset: int | str,
        compact: bool,
        level: str | None,
        slug: str | None,
        order: Literal["newest", "oldest"] = "newest",
        structured: bool = False,
        top_n: int | None = None,
    ) -> dict[str, Any]:
        level = _validate_log_level(level)
        offset = _validate_log_offset(source, offset)
        warnings = _collect_log_warnings(
            source,
            level,
            entity_id,
            device_id,
            context_id,
            end_time,
            slug,
            order,
            offset,
        )
        structured_error_log = structured and source == "error_log"
        if structured and source != "error_log":
            warnings.append(
                "Parameter 'structured' only applies to source='error_log'; "
                f"ignored for source='{source}'"
            )
        if top_n is not None and not structured_error_log:
            # Name the part that is actually missing. On source='error_log' the
            # source is already right and `structured` is the omission, so
            # blaming the source there contradicts the sentence's own opening.
            reason = (
                "ignored because structured=False"
                if source == "error_log"
                else f"ignored for source='{source}'"
            )
            warnings.append(
                "Parameter 'top_n' only applies to source='error_log' with "
                f"structured=True; {reason}"
            )
        _validate_log_slug(source, slug)
        result: dict[str, Any] = await self._fetch_log_source(
            source,
            limit,
            search,
            hours_back,
            entity_id,
            device_id,
            context_id,
            end_time,
            offset,
            compact,
            level,
            slug,
            order,
            structured=structured_error_log,
            top_n=top_n,
        )
        if warnings:
            # Prepend, don't overwrite: the structured error_log path emits its
            # own warnings (format drift, ignored limit/order) and clobbering
            # them would drop the "this is NOT an all-clear" notice.
            result["warnings"] = warnings + result.get("warnings", [])
        return result
