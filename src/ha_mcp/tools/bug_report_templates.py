"""Compatibility exports for the shared report templates."""

from ..runtime.report_templates import _URL_USERINFO_RE as _URL_USERINFO_RE
from ..runtime.report_templates import (
    REPORT_END_MARKER,
    _cut_to_fit,
    _format_client_host_for_template,
    _format_client_info_for_template,
    _format_config_toggles_for_template,
    _format_version_value,
    _generate_bug_title,
    _generate_search_keywords,
    _ReportText,
    _sanitize_log_text,
)

__all__ = [
    "REPORT_END_MARKER",
    "_ReportText",
    "_URL_USERINFO_RE",
    "_cut_to_fit",
    "_format_client_host_for_template",
    "_format_client_info_for_template",
    "_format_config_toggles_for_template",
    "_format_version_value",
    "_generate_bug_title",
    "_generate_search_keywords",
    "_sanitize_log_text",
]
