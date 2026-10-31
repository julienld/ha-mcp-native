"""Bounded lookback policy for a raw window beginning inside a record."""

from .error_log_parsing import _ERROR_LOG_HEADER_LOOKBACK_LINES, _split_log_records
from .log_common import SUPERVISOR_SEARCH_WINDOW_LINES
from .log_window import ErrorLogPage


def wider_error_log_window(page: ErrorLogPage, fetch_lines: int) -> int:
    """Return the one wider fetch allowed to recover a leading record header."""
    if not page.has_more:
        return fetch_lines
    records = _split_log_records(page.text.splitlines())
    if not records or records[0].header is not None:
        return fetch_lines
    return max(
        fetch_lines,
        min(
            fetch_lines + records[0].line_count + _ERROR_LOG_HEADER_LOOKBACK_LINES,
            SUPERVISOR_SEARCH_WINDOW_LINES,
        ),
    )
