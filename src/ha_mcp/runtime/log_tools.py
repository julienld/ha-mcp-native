"""Single public contract for Core logs and the compatibility server."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from pydantic import Field

from .error_log_parsing import _DEFAULT_TOP_N
from .log_common import MAX_LIMIT
from .log_results import _SYSTEM_LOG_MESSAGE_CAP


class LogsFacade:
    def __init__(self, get_logs: Callable[..., Awaitable[dict[str, Any]]]) -> None:
        self.get_logs = get_logs

    async def ha_get_logs(
        self,
        source: Annotated[
            Literal[
                "logbook",
                "system",
                "error_log",
                "supervisor",
                "system_service",
                "logger",
                "fault_log",
            ],
            Field(
                description=(
                    "'logbook': entity state-change history. 'system': HA's "
                    "structured system_log entries (errors, warnings) with full "
                    "exception tracebacks; search also matches those exceptions. "
                    "'error_log': raw log text (home-assistant.log on "
                    "container/pip installs; HA Core's journald stream on "
                    "Supervisor-backed installs), grouped into records: a header "
                    "line starts one and every following line that is not itself "
                    "a header joins it — traceback frames, the blank lines around "
                    "chained exceptions, and non-HA banners such as s6-overlay "
                    "restart lines. 'supervisor': app "
                    "(add-on) container logs (needs slug). 'system_service': "
                    "Supervisor-managed system service logs (needs slug). "
                    "'logger': effective log level per integration (confirms "
                    "ha_set_integration(log_level=...) changes took effect). 'fault_log': HA "
                    "Core's faulthandler crash dump (home-assistant.log.fault), "
                    "written only when HA dies from a native fatal signal, which "
                    "never reaches journald or error_log; empty on a healthy "
                    "install (crash_recorded=False); whole crash blocks are "
                    "ordered with each block's lines kept in place; reads "
                    "through the 'HA-MCP File & YAML Tools' entry."
                )
            ),
        ] = "logbook",
        # Shared parameters
        limit: Annotated[
            int | None,
            Field(
                description=(
                    "Max entries/lines to return. For source='error_log' with "
                    "structured=False it bounds returned lines: whole records "
                    "are selected within it, and a record longer than it is cut "
                    "(header and last lines kept) with a warning naming how many "
                    "lines were cut. Does not apply to source='error_log' with "
                    "structured=True."
                )
            ),
        ] = None,
        search: Annotated[
            str | None,
            Field(
                description=(
                    "Keyword filter on entries/lines. For source='system' it "
                    "matches message, name and exception text; for the raw "
                    "error_log path it matches any line of a record, so a "
                    "traceback frame brings back its header and siblings. "
                    "Structured error_log mode matches message and logger name "
                    "only; source='logger' matches the integration domain."
                )
            ),
        ] = None,
        order: Annotated[
            Literal["newest", "oldest"],
            Field(
                description=(
                    "Sort order for time-ordered sources (logbook, system, error_log, "
                    "supervisor, system_service, fault_log): 'newest' returns "
                    "most-recent first; 'oldest' returns chronological-first. For "
                    "raw-text sources it sets the read direction of the most-recent "
                    "window; fault_log orders whole crash blocks; error_log orders "
                    "whole records and keeps each record's lines in place. Ignored "
                    "for source='logger', and for source='error_log' with "
                    "structured=True."
                )
            ),
        ] = "newest",
        # Source-scoped options: each description names the sources it applies to
        hours_back: Annotated[
            int, Field(ge=1, description="Logbook only: how many hours back to read.")
        ] = 1,
        entity_id: Annotated[
            str | None,
            Field(
                description=(
                    "Logbook only: restrict to one entity, or a comma-separated "
                    "list of entity IDs."
                )
            ),
        ] = None,
        device_id: Annotated[
            str | None,
            Field(
                description=(
                    "Logbook only: native Core device filter - events attributed "
                    "to this device and the context chains they caused. State "
                    "changes of the device's entities are not included; filter by "
                    "entity_id for those. Requires the WebSocket path."
                )
            ),
        ] = None,
        context_id: Annotated[
            str | None,
            Field(
                description=(
                    "Logbook only: only events sharing this context - 'what "
                    "caused this'. Cannot be combined with entity_id or "
                    "device_id; works on both transports when used alone."
                )
            ),
        ] = None,
        end_time: Annotated[
            str | None,
            Field(description="Logbook only: end of the window (ISO datetime)."),
        ] = None,
        offset: Annotated[
            Annotated[int, Field(ge=0)] | str,
            Field(
                description=(
                    "Page deeper into source='logbook', 'error_log' and "
                    "'fault_log' (ignored for other sources). On error_log it "
                    "counts raw log lines back from the newest entry, or is the "
                    "opaque token a previous page returned in 'next_offset' on "
                    "Supervisor/journald installs, which keeps paging exact "
                    "while new log lines arrive (`structured=True` accepts and "
                    "returns that token too); on fault_log it counts lines "
                    "from the start of the assembled crash text. Pass the "
                    "response's 'next_offset' to continue while 'has_more' is "
                    "true."
                ),
            ),
        ] = 0,
        compact: Annotated[
            bool,
            Field(
                description=(
                    f"logbook / system only: save context. Logbook strips "
                    f"attribute dicts; system keeps the start and end of each "
                    f"message string within {_SYSTEM_LOG_MESSAGE_CAP} characters "
                    f"(counted in 'truncated_messages'). search still matches the "
                    f"full message; text inside the cut is not shown. To read one "
                    f"message whole, pass False with a narrow search and limit=1."
                )
            ),
        ] = True,
        # System/error_log-specific
        level: Annotated[
            str | None,
            Field(
                description=(
                    "system / error_log only: keep only entries at exactly this level "
                    "(ERROR, WARNING, INFO, DEBUG, CRITICAL); it is not a threshold. "
                    "For source='error_log' it matches the record's header only, so "
                    "matching text in a traceback frame does not satisfy it."
                )
            ),
        ] = None,
        # error_log-specific: structured summary instead of raw text
        structured: Annotated[
            bool,
            Field(
                description=(
                    "source='error_log' only. When True, return a deduplicated, "
                    "component-grouped summary of the log (counted issues sorted by "
                    "frequency) instead of raw text. Ignored for other sources."
                )
            ),
        ] = False,
        top_n: Annotated[
            int | None,
            Field(
                ge=1,
                description=(
                    f"Max distinct issues to return when structured=True "
                    f"(default {_DEFAULT_TOP_N}, capped at {MAX_LIMIT})."
                ),
            ),
        ] = None,
        # Supervisor + system_service-specific (different namespaces)
        slug: Annotated[
            str | None,
            Field(
                description=(
                    "source='supervisor': app slug, e.g. 'core_mosquitto' (use "
                    "ha_get_app() to list installed slugs). "
                    "source='system_service': service name, one of supervisor, "
                    "host, core, dns, audio, multicast — here "
                    "'supervisor' is the Supervisor service's own logs, not an "
                    "app with that name."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Get Home Assistant logs from various sources.

        Prefer source='system' for triage: it returns HA's own deduplicated
        system_log entries with counts, first_occurred and the full exception
        traceback, and its counts run since each error first occurred. error_log with
        structured=True counts only what is inside the fetched window (reported
        as window_start/window_end; every install reads a capped window) and
        drops tracebacks; structured=False returns the raw records instead, so
        it covers what falls outside system_log's WARNING+ in-memory buffer. Use
        structured=True for the per-component rollup; in structured mode
        limit/order do not apply: issues are ranked by count, then severity,
        then recency, over a fixed deep window.

        Raw-text sources (error_log, supervisor, system_service) read a bounded
        window per call, so level/search filter and limit slice within that
        window only; window_lines reports the size requested. Logbook
        responses carry has_more plus a pagination_hint; error_log and fault_log
        carry has_more with a next_offset to pass back while it stays true.
        """
        return await self.get_logs(
            source=source,
            limit=limit,
            search=search,
            hours_back=hours_back,
            entity_id=entity_id,
            device_id=device_id,
            context_id=context_id,
            end_time=end_time,
            offset=offset,
            compact=compact,
            level=level,
            slug=slug,
            order=order,
            structured=structured,
            top_n=top_n,
        )
