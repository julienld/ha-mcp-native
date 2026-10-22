"""Compatibility names for the pure error-log parser."""

from ha_mcp.backends.legacy import legacy_errors_sync
from ha_mcp.runtime import error_log_parsing as _shared

_HA_LOG_LINE_RE = _shared._HA_LOG_LINE_RE
_MAX_MESSAGE_LEN = _shared._MAX_MESSAGE_LEN
_COMPONENT_PREFIX_DEPTH = _shared._COMPONENT_PREFIX_DEPTH
_CUSTOM_COMPONENT_PREFIX_DEPTH = _shared._CUSTOM_COMPONENT_PREFIX_DEPTH
_DEFAULT_TOP_N = _shared._DEFAULT_TOP_N
_MAX_COMPONENTS = _shared._MAX_COMPONENTS
_TRUNCATION_MARK = _shared._TRUNCATION_MARK
_LEVEL_SEVERITY = _shared._LEVEL_SEVERITY
_EMPTY_FETCH_WARNING = _shared._EMPTY_FETCH_WARNING
_ANSI_RE = _shared._ANSI_RE
_strip_ansi = _shared._strip_ansi
_normalize_timestamp = _shared._normalize_timestamp
_get_component_prefix = _shared._get_component_prefix
_truncate_message = _shared._truncate_message
_ExtractedLines = _shared._ExtractedLines
_extract_log_entries = _shared._extract_log_entries
_parse_error_log_structured = _shared._parse_error_log_structured
STRUCTURED_ERROR_LOG_WINDOW_LINES = _shared.STRUCTURED_ERROR_LOG_WINDOW_LINES
_HISTORY_START_NOTE = _shared._HISTORY_START_NOTE
_ErrorLogWindow = _shared._ErrorLogWindow
_error_log_window_lines = legacy_errors_sync(_shared._error_log_window_lines)
_next_page_step = _shared._next_page_step
_build_error_log_pagination_hint = _shared._build_error_log_pagination_hint
_attach_error_log_pagination = _shared._attach_error_log_pagination
