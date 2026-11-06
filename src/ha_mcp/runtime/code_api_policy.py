"""Shared saved-script path normalization and protected-write policies."""

import urllib.parse
from typing import Any

# Path-prefix denylist for ``_api_post``. Two flavours of entry, kept in
# one list because the matching logic is identical:
#   1. Endpoints with no legitimate sandbox use case at all — currently
#      just ``states/`` (raw state writes can conjure ghost entities and
#      override real ones in the in-memory state machine).
#   2. Endpoints whose corresponding wrapping MCP tool performs
#      validation / lint / hash-locking that raw ``api_post`` would skip
#      — currently ``config/{automation,script,scene}/config/``.
# The prefixes are matched after ``_normalize_endpoint`` strips the
# leading ``api/`` so they are written as plain HA-relative paths.
_API_POST_BLOCKED_PREFIXES: tuple[tuple[str, str, str], ...] = (
    (
        "states/",
        "Direct writes to /api/states/<entity_id>",
        "use the appropriate service via call_tool('ha_call_service', ...) "
        "so the change goes through the integration's state machine",
    ),
    (
        "config/automation/config/",
        "Direct writes to /api/config/automation/config/*",
        "use call_tool('ha_config_set_automation', ...) so schema "
        "validation, reference checks, and hash-locking run",
    ),
    (
        "config/script/config/",
        "Direct writes to /api/config/script/config/*",
        "use call_tool('ha_config_set_script', ...)",
    ),
    (
        "config/scene/config/",
        "Direct writes to /api/config/scene/config/*",
        "use call_tool('ha_config_set_scene', ...) so the entities-must-be-dict "
        "shape check, reference validation, and hash-locking run",
    ),
)

# HA Core internal events. Firing one of these via POST /api/events/<name>
# spoofs HA's own bookkeeping bus and can fan out into user automations
# listening for ``state_changed`` / ``automation_reloaded`` / etc. without
# the real subsystem ever having fired. Custom event types stay allowed —
# only the names HA Core itself emits are blocked.
_BLOCKED_HA_INTERNAL_EVENTS: frozenset[str] = frozenset(
    {
        "state_changed",
        "service_registered",
        "service_removed",
        "service_executed",
        "automation_reloaded",
        "script_started",
        "script_finished",
        "homeassistant_start",
        "homeassistant_started",
        "homeassistant_stop",
        "homeassistant_close",
        "homeassistant_final_write",
        "core_config_updated",
        "device_registry_updated",
        "entity_registry_updated",
        "area_registry_updated",
        "category_registry_updated",
        "floor_registry_updated",
        "label_registry_updated",
        # ``logbook_entry`` is the documented logbook write API — the Logbook
        # integration consumes it to render rows. Sandbox code firing this
        # event would inject attacker-fabricated rows directly into the
        # user's primary investigation tool, which is a data-integrity issue.
        "logbook_entry",
        "lovelace_updated",
        "panels_updated",
        "themes_updated",
        "component_loaded",
        "recorder_5min_statistics_generated",
        "recorder_hourly_statistics_generated",
    }
)


def api_post_policy_refusal(normalized: str) -> str | None:
    """Return a protected-write rejection, or ``None`` for separate route admission.

    ``normalized`` is the path after ``_normalize_endpoint`` stripped any
    leading ``/`` and ``api/`` prefix, so for example
    ``"/api/states/sun.sun"`` arrives here as ``"states/sun.sun"``.
    """
    for prefix, what, alternative in _API_POST_BLOCKED_PREFIXES:
        if normalized.startswith(prefix) or normalized == prefix.rstrip("/"):
            return f"{what} are blocked from the sandbox; {alternative}."
    if normalized.startswith("events/"):
        event_name = normalized[len("events/") :]
        if event_name in _BLOCKED_HA_INTERNAL_EVENTS:
            return (
                f"Firing HA-internal event {event_name!r} from the sandbox "
                "is blocked because it can spoof HA's own bookkeeping and "
                "trigger user automations without the underlying real "
                "event ever happening. Custom event types are allowed."
            )
    return None


def _normalize_endpoint(endpoint: Any) -> str:
    """Normalize a path-only endpoint to be relative to the httpx base URL.

    Strips leading slashes and any accidental ``api/`` prefix so the same
    path works whether the caller wrote ``"events"``, ``"/events"``, or
    ``"/api/events"``.

    Rejects:

    * Absolute URL forms — ``://``, leading ``//`` (protocol-relative),
      or ``@`` before the first ``/`` (userinfo). Without this httpx
      will dispatch the request to the absolute host *with the HA
      bearer token still attached*, leaking credentials.

      Note: ``@`` later in the path (``events/foo@bar``) is fine —
      only userinfo position (before the first ``/``) is the
      credential-leaking shape that http URL parsers will treat as
      ``user@host``.
    * ``..`` path segments — httpx happily resolves
      ``base_url='http://ha:8123/api'`` + endpoint ``'../auth/providers'``
      to ``http://ha:8123/auth/providers``, escaping the ``/api/``
      prefix entirely. HA exposes other bearer-authenticated routes
      at root (``/auth/...``, ``/profile``, etc.) — every one of
      those becomes reachable from the sandbox unless we reject
      ``..`` here. Each segment is also percent-decoded once before
      the comparison so ``%2e%2e`` (and other percent-encoded
      variants of ``..``) can't slip past on reverse-proxy setups
      that decode-then-resolve.
    """
    if not isinstance(endpoint, str):
        raise ValueError("endpoint must be a string path (e.g. '/states')")
    if "://" in endpoint or endpoint.startswith("//"):
        raise ValueError(
            "endpoint must be a HA-relative path; absolute URLs are blocked"
        )
    first_slash = endpoint.find("/")
    userinfo_marker = endpoint.find("@")
    if userinfo_marker >= 0 and (first_slash < 0 or userinfo_marker < first_slash):
        raise ValueError("endpoint must not contain userinfo")
    ep = endpoint.lstrip("/")
    if ep.startswith("api/"):
        ep = ep[4:]
    # ``..`` segments would let the sandbox escape the ``/api/`` prefix
    # via httpx URL resolution. Check after stripping so the comparison
    # is against actual path segments, and percent-decode each segment
    # so encoded forms (``%2e%2e`` and similar) don't slip past on
    # reverse-proxy setups that decode-then-resolve.
    for segment in ep.split("/"):
        if urllib.parse.unquote(segment) == "..":
            raise ValueError(
                "endpoint must not contain '..' path segments "
                "(including percent-encoded forms like %2e%2e); "
                "the sandbox is restricted to /api/ routes"
            )
    return ep
