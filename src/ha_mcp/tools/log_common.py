"""Compatibility imports for the shared log query policy."""

from ha_mcp._version import is_running_in_addon
from ha_mcp.backends.legacy import legacy_errors_sync
from ha_mcp.runtime import log_common as _shared

COMPACT_LOGBOOK_FIELDS = _shared.COMPACT_LOGBOOK_FIELDS
SYSTEM_SERVICE_SLUGS = _shared.SYSTEM_SERVICE_SLUGS
DEFAULT_LIMIT = _shared.DEFAULT_LIMIT
DEFAULT_LOG_LIMIT = _shared.DEFAULT_LOG_LIMIT
SUPERVISOR_SEARCH_WINDOW_LINES = _shared.SUPERVISOR_SEARCH_WINDOW_LINES
MAX_LIMIT = _shared.MAX_LIMIT
_LOG_LEVEL_RE = _shared._LOG_LEVEL_RE
VALID_LOG_LEVELS = _shared.VALID_LOG_LEVELS
_compact_logbook_entries = _shared._compact_logbook_entries
_coerce_limit = legacy_errors_sync(_shared._coerce_limit)


def _addon_auth_error_suggestions() -> list[str]:
    """What a log read adds to the classified auth guidance, which comes first."""
    if is_running_in_addon():
        return [
            "Verify SUPERVISOR_TOKEN is set inside the app (add-on), and reinstall "
            "the app if the token may have rotated"
        ]
    return [
        "Reading logs needs an admin user's token: check that the Long-Lived Access "
        "Token belongs to an admin (Settings → Profile → Long-Lived Access Tokens)",
    ]


__all__ = [
    "_coerce_limit",
    "_compact_logbook_entries",
]
