"""Compatibility names for the pure error-log parser."""

from ha_mcp.backends.legacy import legacy_errors_sync
from ha_mcp.runtime import error_log_parsing as _shared

_MAX_MESSAGE_LEN = _shared._MAX_MESSAGE_LEN
_DEFAULT_TOP_N = _shared._DEFAULT_TOP_N
_MAX_COMPONENTS = _shared._MAX_COMPONENTS
_TRUNCATION_MARK = _shared._TRUNCATION_MARK
_get_component_prefix = _shared._get_component_prefix
_parse_error_log_structured = _shared._parse_error_log_structured
STRUCTURED_ERROR_LOG_WINDOW_LINES = _shared.STRUCTURED_ERROR_LOG_WINDOW_LINES
_error_log_window_lines = legacy_errors_sync(_shared._error_log_window_lines)


__all__ = [
    "_DEFAULT_TOP_N",
    "_MAX_COMPONENTS",
    "_MAX_MESSAGE_LEN",
    "_TRUNCATION_MARK",
    "_error_log_window_lines",
    "_get_component_prefix",
    "_parse_error_log_structured",
]
