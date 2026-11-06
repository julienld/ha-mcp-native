"""Shared logger-level naming, unknown-state and optional-read handling."""

import logging
from typing import Any

from ha_mcp.errors import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)

logger = logging.getLogger(__name__)


_LOG_LEVEL_NAMES: dict[int, str] = {
    0: "NOTSET",
    10: "DEBUG",
    20: "INFO",
    30: "WARNING",
    40: "ERROR",
    50: "CRITICAL",
}


def normalize_log_level(level: Any) -> str | None:
    """Normalize a numeric or string log level to its canonical uppercase name.

    Returns None if the value can't be recognized as a log level.
    """
    if isinstance(level, bool):  # bool is an int subclass — reject explicitly
        return None
    if isinstance(level, int):
        return _LOG_LEVEL_NAMES.get(level, f"LEVEL_{level}")
    if isinstance(level, str):
        stripped = level.strip().upper()
        if not stripped:
            return None
        return stripped
    return None


async def get_logger_levels(
    client: Any, warnings: list[str] | None = None
) -> dict[str, dict[str, Any]]:
    """Fetch current HA integration log levels via the ``logger/log_info`` WS command.

    Returns a mapping of integration domain (e.g. ``"mqtt"``) to a dict with:

    - ``name``: canonical level name (``"DEBUG"``, ``"INFO"``, ``"WARNING"``,
      ``"ERROR"``, ``"CRITICAL"``, ``"NOTSET"``, or ``"LEVEL_<n>"`` for
      non-standard ints).
    - ``raw``: the original numeric level (``int``) when HA returned an int,
      otherwise ``None`` (e.g. when the level was already provided as a string).

    Best-effort enrichment: returns an empty dict on connection/IO failures.
    An empty map alone cannot say whether Home Assistant has no custom levels
    or whether nobody could ask, so a caller that passes ``warnings`` gets a
    line on failure and can report UNKNOWN instead of claiming DEFAULT (#1947).
    Programming errors are not suppressed — they surface as bugs during
    development/CI.
    """
    try:
        result = await client.backend.execute(
            "integrations.log_levels", {}, client.context
        )
    except (
        HomeAssistantConnectionError,
        HomeAssistantAPIError,
        HomeAssistantAuthError,
        TimeoutError,
        OSError,
    ) as exc:
        logger.debug("logger/log_info fetch failed: %s", exc)
        if warnings is not None:
            warnings.append(f"log levels unavailable: {exc}")
        return {}

    if not isinstance(result, dict) or not result.get("success"):
        if warnings is not None:
            warnings.append("log levels unavailable: logger/log_info failed")
        return {}

    entries = result.get("result", [])
    if not isinstance(entries, list):
        return {}

    levels: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        domain = entry.get("domain")
        if not isinstance(domain, str) or not domain:
            continue
        raw_level = entry.get("level")
        name = normalize_log_level(raw_level)
        if name is None:
            continue
        levels[domain] = {
            "name": name,
            "raw": raw_level
            if isinstance(raw_level, int) and not isinstance(raw_level, bool)
            else None,
        }
    return levels
