"""Shared system/error/logger log projections; no I/O or SDK context."""

from typing import Any, Literal

from .error_log_parsing import (
    _DEFAULT_TOP_N,
    _EMPTY_FETCH_WARNING,
    _attach_error_log_pagination,
    _ErrorLogWindow,
    _next_page_step,
    _parse_error_log_structured,
)
from .integration_logs import normalize_log_level
from .log_common import _LOG_LEVEL_RE, DEFAULT_LOG_LIMIT, _coerce_limit
from .log_window import ErrorLogPage

# system_log keeps a message verbatim, so one warning that reprs a binary
# payload (a blocking-call report on write_bytes, say) can run to hundreds of
# kilobytes. The cap is per string, and an entry keeps up to five messages, so
# one entry can still return about five times the cap.
_SYSTEM_LOG_MESSAGE_CAP = 2000


def _truncation_marker(cut: int) -> str:
    return (
        f"…[{cut:,} chars cut; compact=False with a narrow search and "
        f"limit=1 returns it whole]…"
    )


def _cap_message(message: str) -> tuple[str, bool]:
    """Keep both ends of an oversized message, the marker in between.

    The lead names what happened and the end carries the innermost stack
    frames that identify the offender; the middle is usually the payload.
    """
    if len(message) <= _SYSTEM_LOG_MESSAGE_CAP:
        return message, False
    cut = len(message)
    # The marker's width depends on the digits of the cut size, so settle it.
    while True:
        budget = _SYSTEM_LOG_MESSAGE_CAP - len(_truncation_marker(cut))
        settled = len(message) - budget
        if settled == cut:
            break
        cut = settled
    head = budget // 2
    tail = budget - head
    return message[:head] + _truncation_marker(cut) + message[-tail:], True


def _cap_system_log_messages(entries: list[Any]) -> int:
    """Cap oversized system_log message strings in place; return how many."""
    capped = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        message = entry.get("message")
        if isinstance(message, str):
            entry["message"], cut = _cap_message(message)
            capped += cut
        elif isinstance(message, list):
            parts = []
            for part in message:
                if isinstance(part, str):
                    kept, cut = _cap_message(part)
                    capped += cut
                    parts.append(kept)
                else:
                    parts.append(part)
            entry["message"] = parts
    return capped


class LogResults:
    @staticmethod
    def _system_log_sort_key(entry: Any) -> float:
        """Total-order-safe sort key for system_log entries.

        ``system_log/list`` does not guarantee a numeric ``timestamp`` on every
        record. Coerce a missing / non-numeric / non-dict entry to ``0.0`` so
        sorting never raises a cross-type ``TypeError`` (bools are excluded so
        a stray ``True`` doesn't read as ``1.0``).
        """
        if not isinstance(entry, dict):
            return 0.0
        ts = entry.get("timestamp")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            return 0.0
        return float(ts)

    def _build_structured_error_log(
        self,
        raw_log: str,
        search: str | None,
        level: str | None,
        top_n: int | None,
        limit: int | None,
        order: Literal["newest", "oldest"],
    ) -> dict[str, Any]:
        """Summarise the raw error log and annotate what shaped the result."""
        effective_top_n = _coerce_limit(
            top_n,
            default=_DEFAULT_TOP_N,
            suggestion_example="20",
            param_name="top_n",
        )
        result = _parse_error_log_structured(
            raw_log,
            search=search,
            level=level,
            top_n=effective_top_n,
        )
        # Report the filters that shaped the summary, matching the raw path —
        # otherwise an empty summary is indistinguishable from a quiet log.
        structured_filters = {
            k: v for k, v in (("level", level), ("search", search)) if v
        }
        if structured_filters:
            result["filters_applied"] = structured_filters
        # `limit`/`order` are accepted for signature compatibility but do nothing
        # here; say so rather than silently ignoring them, which is the
        # convention this tool already uses elsewhere.
        ignored = [
            name
            for name, given in (
                ("limit", limit is not None),
                ("order", order != "newest"),
            )
            if given
        ]
        if ignored:
            # Append: the parser may already have warned about format drift, and
            # overwriting that would drop the "this is NOT an all-clear" notice.
            result.setdefault("warnings", []).append(
                f"Parameter(s) {', '.join(ignored)} do not apply when "
                "structured=True; the summary ranks the whole fetched window "
                "by occurrence count. Use top_n to bound the output."
            )
        return result

    @staticmethod
    def _filter_error_log_lines(
        lines: list[str], search: str | None, level: str | None
    ) -> tuple[list[tuple[int, str]], dict[str, str]]:
        """Filter window lines, keeping each survivor's index in the window.

        The index is what makes paging exact: the next page has to resume where
        the returned block starts, not a whole window further back, or a filter
        that drops most of a window silently skips everything it dropped.
        """
        indexed = list(enumerate(lines))
        filters_applied: dict[str, str] = {}

        if level:

            def _line_has_level(ln: str, target: str) -> bool:
                m = _LOG_LEVEL_RE.search(ln)
                return m is not None and m.group(1).upper() == target

            indexed = [(i, ln) for i, ln in indexed if _line_has_level(ln, level)]
            filters_applied["level"] = level

        if search:
            search_lower = search.lower()
            indexed = [(i, ln) for i, ln in indexed if search_lower in ln.lower()]
            filters_applied["search"] = search

        return indexed, filters_applied

    def _build_raw_error_log(
        self,
        raw_log: str,
        search: str | None,
        level: str | None,
        effective_limit: int,
        order: Literal["newest", "oldest"],
        window: _ErrorLogWindow,
    ) -> tuple[dict[str, Any], int]:
        """Return the most recent matching lines, plus the next page's step.

        The step is how far back ``offset`` must move to resume just before the
        oldest line returned here. ``effective_limit`` arrives already coerced:
        the caller needs the same value for the pagination hint, and the
        summary path must not coerce it at all.
        """
        matches, filters_applied = self._filter_error_log_lines(
            raw_log.splitlines(), search, level
        )

        total_lines = len(matches)
        # Always take the most-recent window (the tail of the chronological
        # file); 'order' controls only the display direction of that window.
        shown = matches[-effective_limit:]
        step = _next_page_step(window, shown)
        lines = [ln for _, ln in shown]
        if order == "newest":
            lines = list(reversed(lines))

        data: dict[str, Any] = {
            "success": True,
            "source": "error_log",
            "log": "\n".join(lines),
            "total_lines": total_lines,
            "returned_lines": len(lines),
            "limit": effective_limit,
            "window_lines": window.fetch_lines,
            "order": order,
            "note": (
                "Returned the most recent log lines matching filters, from a "
                "bounded window of the log. 'offset' counts raw log lines back "
                "from the newest entry; filters apply within each fetched "
                "window, so 'total_lines' counts matches in this window only"
            ),
        }
        if filters_applied:
            data["filters_applied"] = filters_applied
        return data, step

    @staticmethod
    def _apply_empty_window_warning(
        data: dict[str, Any], raw_text: str, offset: int, structured: bool
    ) -> None:
        """Tell an empty terminal page apart from an empty fetch.

        At ``offset=0`` an empty response is the alarming case: a running
        instance always logs something, so nothing at the newest edge means the
        fetch produced nothing — without saying so, an agent reports "no errors
        in the log". Deeper into history an empty window is just the end of the
        record, and ``_EMPTY_FETCH_WARNING`` would send the caller after an
        outage that is not happening; the structured parser emits it
        unconditionally, so it is replaced there rather than contradicted.
        """
        if raw_text.strip():
            return
        if offset:
            kept = [
                w for w in data.get("warnings", []) if _EMPTY_FETCH_WARNING not in w
            ]
            data["warnings"] = kept + [
                f"No log content at offset={offset}: the recorded history ends "
                "before this window. Lower 'offset' to reach the entries that exist."
            ]
        elif not structured:
            # The structured path's parser emits its own version of this.
            data.setdefault("warnings", []).append(
                f"The fetch returned no log content. {_EMPTY_FETCH_WARNING}"
            )

    @staticmethod
    def _parse_logger_entry(entry: Any) -> dict[str, Any] | None:
        if not isinstance(entry, dict):
            return None
        domain = entry.get("domain")
        if not isinstance(domain, str) or not domain:
            return None
        raw_level = entry.get("level")
        level_name = normalize_log_level(raw_level)
        if level_name is None:
            return None
        return {
            "domain": domain,
            "level": level_name,
            "level_raw": raw_level if isinstance(raw_level, int) else None,
        }

    def system_log_result(
        self,
        entries: Any,
        effective_limit: int,
        search: str | None,
        level: str | None,
        order: str,
        compact: bool,
    ) -> dict[str, Any]:
        if not isinstance(entries, list):
            entries = []

        filters_applied: dict[str, str] = {}

        # The isinstance guards keep a malformed non-dict record from
        # raising AttributeError out of this method's narrow except
        # clauses without the structured ToolError envelope. Unfiltered
        # calls still return such records verbatim (pinned by
        # test_tolerates_missing_none_and_non_dict_entries); a filter
        # drops them, since they cannot carry the field being matched.
        if level:
            entries = [
                e
                for e in entries
                if isinstance(e, dict) and str(e.get("level", "")).upper() == level
            ]
            filters_applied["level"] = level

        if search:
            search_lower = search.lower()
            entries = [
                e
                for e in entries
                if isinstance(e, dict)
                and (
                    search_lower in str(e.get("message", "")).lower()
                    or search_lower in str(e.get("name", "")).lower()
                )
            ]
            filters_applied["search"] = search

        # system_log/list entries carry a 'timestamp' (epoch float, last
        # occurrence), but HA does not guarantee it on every record. Sort
        # with a total-order-safe key so 'order' is deterministic regardless
        # of HA's native ordering (newest-first by default) and a missing /
        # non-numeric / non-dict entry can never raise a cross-type
        # TypeError out of this method's narrow except clause.
        entries.sort(
            key=self._system_log_sort_key,
            reverse=(order == "newest"),
        )

        total_entries = len(entries)
        entries = entries[:effective_limit]
        truncated_messages = _cap_system_log_messages(entries) if compact else 0

        data: dict[str, Any] = {
            "success": True,
            "source": "system",
            "entries": entries,
            "total_entries": total_entries,
            "returned_entries": len(entries),
            "limit": effective_limit,
            "order": order,
        }
        if filters_applied:
            data["filters_applied"] = filters_applied
        if truncated_messages:
            data["truncated_messages"] = truncated_messages

        return data

    def logger_result(
        self, raw_entries: Any, effective_limit: int, search: str | None
    ) -> dict[str, Any]:
        if not isinstance(raw_entries, list):
            raw_entries = []

        loggers: list[dict[str, Any]] = []
        for entry in raw_entries:
            parsed = self._parse_logger_entry(entry)
            if parsed is not None:
                loggers.append(parsed)

        filters_applied: dict[str, str] = {}
        if search:
            search_lower = search.lower()
            loggers = [
                entry for entry in loggers if search_lower in entry["domain"].lower()
            ]
            filters_applied["search"] = search

        loggers.sort(key=lambda entry: entry["domain"])

        total_entries = len(loggers)
        loggers = loggers[:effective_limit]

        data: dict[str, Any] = {
            "success": True,
            "source": "logger",
            "loggers": loggers,
            "total_entries": total_entries,
            "returned_entries": len(loggers),
            "limit": effective_limit,
        }
        if filters_applied:
            data["filters_applied"] = filters_applied

        return data

    def error_log_result(
        self,
        page: ErrorLogPage,
        fetch_lines: int,
        limit: int | None,
        search: str | None,
        level: str | None,
        order: Literal["newest", "oldest"],
        offset: int,
        structured: bool,
        top_n: int | None,
    ) -> dict[str, Any]:
        raw_text = page.text or ""
        window = _ErrorLogWindow(
            offset=offset,
            fetch_lines=fetch_lines,
            raw_line_count=len(raw_text.splitlines()),
            has_more=page.has_more,
        )

        if structured:
            data = self._build_structured_error_log(
                raw_text,
                search=search,
                level=level,
                top_n=top_n,
                limit=limit,
                order=order,
            )
            data["window_lines"] = fetch_lines
            # The summary consumes the whole window, so the next page
            # starts a whole window further back.
            step = fetch_lines
            effective_limit = None
        else:
            effective_limit = _coerce_limit(
                limit, default=DEFAULT_LOG_LIMIT, suggestion_example="100"
            )
            data, step = self._build_raw_error_log(
                raw_text,
                search=search,
                level=level,
                effective_limit=effective_limit,
                order=order,
                window=window,
            )
        self._apply_empty_window_warning(data, raw_text, offset, structured)
        _attach_error_log_pagination(
            data,
            window,
            step,
            effective_limit,
            search,
            level,
            order,
            structured,
            top_n,
            # Raw path only: the structured summary consumes the whole
            # window, so it never leaves matches behind a limit slice.
            unreturned_matches=(
                0 if structured else data["total_lines"] - data["returned_lines"]
            ),
        )
        return data


def supervisor_log_result(
    log_text: str,
    source: str,
    slug: str,
    effective_limit: int,
    search: str | None,
    order: Literal["newest", "oldest"],
) -> dict[str, Any]:
    lines = log_text.splitlines() if log_text else []

    filters_applied: dict[str, str] = {}

    if search:
        search_lower = search.lower()
        lines = [ln for ln in lines if search_lower in ln.lower()]
        filters_applied["search"] = search

    total_lines = len(lines)
    # Always take the most-recent window (the tail); 'order' controls
    # only the display direction of that window.
    lines = lines[-effective_limit:]
    if order == "newest":
        lines = list(reversed(lines))

    data: dict[str, Any] = {
        "success": True,
        "source": source,
        "slug": slug,
        "log": "\n".join(lines),
        "total_lines": total_lines,
        "returned_lines": len(lines),
        "limit": effective_limit,
        "order": order,
    }
    if filters_applied:
        data["filters_applied"] = filters_applied

    return data
