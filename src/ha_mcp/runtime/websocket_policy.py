"""Shared raw-command admission; native mode still has no raw transport."""

import posixpath
import urllib.parse
from collections.abc import Mapping
from typing import Any, NamedTuple

from ha_mcp.errors import create_validation_error

from .action_params import _parse_json_dict_param
from .errors import raise_failure

BLOCKED_WS_WRITE_COMMANDS: frozenset[str] = frozenset(
    {
        "config/core/update",
        "lovelace/config/save",
        "ha_mcp_tools/dashboard_edit",
        "lovelace/dashboards/create",
        "lovelace/dashboards/delete",
        "lovelace/dashboards/update",
        # Resource writes carry the same wrapping-tool validation as the
        # dashboard commands above -- auto-backup, the #1072 HA-config-YAML
        # misroute rejection, the inline size cap, and the data:-URL routing
        # guard all live in ha_config_set_dashboard_resource (#2060).
        "lovelace/resources/create",
        "lovelace/resources/delete",
        "lovelace/resources/update",
        "config/area_registry/delete",
        "config/area_registry/disable",
        "config/area_registry/update",
        "config/device_registry/delete",
        "config/device_registry/disable",
        "config/device_registry/update",
        "config/device_registry/remove",
        "config/device_registry/remove_config_entry",
        "config/entity_registry/delete",
        "config/entity_registry/disable",
        "config/entity_registry/update",
        "config/entity_registry/remove",
        "config/floor_registry/create",
        "config/floor_registry/delete",
        "config/floor_registry/update",
        "config/label_registry/create",
        "config/label_registry/delete",
        "config/label_registry/update",
        "config/category_registry/create",
        "config/category_registry/delete",
        "config/category_registry/update",
    }
)

# WebSocket commands that mint, list or revoke credentials, or manage users and
# their passwords (`auth/long_lived_access_token` answers with a brand-new
# long-lived token in plain text). A raw escape hatch has no use case that
# needs them, and their replies would land verbatim in the model context and its
# transcript. Matched by prefix so a credential command added to HA Core later
# is refused by default; `auth/current_user` (whoami) is the one read that stays
# open.
_CREDENTIAL_WS_PREFIXES: tuple[str, ...] = (
    "auth/",
    "config/auth/",
    "config/auth_provider/",
)
_CREDENTIAL_WS_ALLOWED: frozenset[str] = frozenset({"auth/current_user"})
# Supervisor endpoints reachable through Core's `supervisor/api` relay with the
# same effect: `/auth/*` lists users and resets their passwords, and
# `/os/ssh/authorized_keys` adds a host SSH key. Each covers its subtree.
_CREDENTIAL_SUPERVISOR_ENDPOINTS: tuple[str, ...] = (
    "/auth",
    "/os/ssh/authorized_keys",
)


def normalized_path(path: str) -> str:
    """Fold percent-escapes, ``//``, ``.`` and ``..`` so an equivalent spelling
    cannot slip past (HTTP clients decode ``%62ackups`` to ``backups``).

    Leading slashes are stripped first: POSIX normpath keeps exactly two.
    """
    decoded = urllib.parse.unquote(path.split("?", 1)[0])
    stripped = decoded.strip().lower().lstrip("/")
    return posixpath.normpath("/" + stripped)


def supervisor_path(endpoint: str) -> str:
    """``normalized_path`` of a Supervisor endpoint, with the ``/v2`` mount folded off.

    Supervisor mounts the same handlers again under ``/v2`` when its
    supervisor_v2_api feature flag is on (its own security middleware matches
    both with ``^(?:/v2)?``), and the flag itself can be switched on through
    ``supervisor/api``. Every guard on a Supervisor path classifies this form.
    """
    path = normalized_path(endpoint)
    if path == "/v2" or path.startswith("/v2/"):
        path = path[3:] or "/"
    return path


def is_credential_ws_command(
    command: str, params: Mapping[str, Any] | None = None
) -> bool:
    """True when ``command`` (with its message ``params``) mints, lists or
    revokes credentials, manages users, or adds host SSH keys."""
    normalized = command.strip().lower()
    if normalized == "supervisor/api":
        endpoint = (params or {}).get("endpoint")
        if not isinstance(endpoint, str):
            return False
        path = supervisor_path(endpoint)
        return any(
            path == root or path.startswith(root + "/")
            for root in _CREDENTIAL_SUPERVISOR_ENDPOINTS
        )
    if normalized in _CREDENTIAL_WS_ALLOWED:
        return False
    return normalized.startswith(_CREDENTIAL_WS_PREFIXES)


class WsRefusal(NamedTuple):
    """Why a raw WebSocket surface refuses a command, and what to do instead."""

    message: str
    suggestions: list[str]

    def as_text(self) -> str:
        return " ".join([self.message, *self.suggestions])


_POLICY_REFUSAL_SUGGESTIONS: list[str] = [
    (
        "This is a fixed policy refusal, not an input error: no other spelling, "
        "parameter or ha-mcp tool sends it."
    ),
    (
        "Ask the user to make the change in the Home Assistant UI (Profile > "
        "Security for tokens, Settings > People for users and passwords)."
    ),
]


def raw_ws_refusal(
    command: str,
    params: Mapping[str, Any] | None = None,
    *,
    sandbox: bool = False,
) -> WsRefusal | None:
    """Why a raw WebSocket surface refuses ``command``, or ``None`` to send it.

    One rule set for ha_call_service's ``ws_command`` (``sandbox=False``) and the
    Code Mode ``ws_send`` bridge (``sandbox=True``), worded for each surface.
    ``params`` are the message fields besides ``type``; ``supervisor/api`` is
    judged by its ``endpoint``.
    """
    if command.strip().lower() in BLOCKED_WS_WRITE_COMMANDS:
        if sandbox:
            return WsRefusal(
                f"WebSocket command {command!r} is blocked from the sandbox.",
                [
                    "Use the corresponding wrapping tool via call_tool (e.g. "
                    "ha_config_set_dashboard, ha_set_area_or_floor, "
                    "ha_set_device, ha_set_entity) so validation runs."
                ],
            )
        return WsRefusal(
            f"ws_command '{command}' mutates persistent state that a "
            "dedicated tool guards with backups and conflict checks.",
            [
                "Use the corresponding ha-mcp tool (e.g. ha_config_set_dashboard, "
                "ha_set_area_or_floor, ha_remove_entity) instead."
            ],
        )
    if is_credential_ws_command(command, params):
        return WsRefusal(
            f"WebSocket command {command!r} is refused by policy: it mints, "
            "lists or revokes credentials, manages users, or adds host SSH keys.",
            list(_POLICY_REFUSAL_SUGGESTIONS),
        )
    return None


_WS_COMMAND_EVENT_BLOCKLIST = frozenset(
    {
        "render_template",  # event-based; use ha_eval_template instead
        "system_health/info",  # two-phase (see tools_system._fetch_health_info)
        "assist_pipeline/run",  # streams pipeline events
    }
)

_WS_STREAMING_SUBSTRINGS = ("subscribe", "stream", "start_preview")

_WS_COMMAND_SERVICE_INVOKERS = frozenset({"call_service", "execute_script"})

_WS_RESERVED_ENVELOPE_KEYS = frozenset({"type", "id"})


def _is_streaming_ws_command(command_type: str) -> bool:
    """Return True for subscription / streaming / two-phase WS commands."""
    lowered = command_type.lower()
    if lowered in _WS_COMMAND_EVENT_BLOCKLIST:
        return True
    return any(sub in lowered for sub in _WS_STREAMING_SUBSTRINGS)


def validate_ws_request(
    ws_command: str, data: Any, *, domain: str | None, service: str | None
) -> tuple[str, dict[str, Any]]:
    command_type = ws_command.strip()
    if domain is not None or service is not None:
        raise_failure(
            create_validation_error(
                "Provide either domain + service (a registered service call) "
                "OR ws_command (a raw WebSocket command), not both.",
                parameter="ws_command",
            )
        )
    if not command_type:
        raise_failure(
            create_validation_error(
                "ws_command must be a non-empty WebSocket command type "
                "(the message's 'type' field).",
                parameter="ws_command",
            )
        )
    if _is_streaming_ws_command(command_type):
        raise_failure(
            create_validation_error(
                f"ws_command '{command_type}' is a streaming or two-phase "
                "command; ha_call_service only sends one-shot "
                "request/response commands. For template rendering use "
                "ha_eval_template.",
                parameter="ws_command",
            )
        )
    if command_type.lower() in _WS_COMMAND_SERVICE_INVOKERS:
        raise_failure(
            create_validation_error(
                f"ws_command '{command_type}' invokes Home Assistant services "
                "and would bypass ha_call_service's safeguards. Use the "
                "domain/service parameters for service calls instead.",
                parameter="ws_command",
            )
        )
    if command_type.lower().startswith("ha_mcp_tools/"):
        raise_failure(
            create_validation_error(
                "ha_call_service cannot invoke 'ha_mcp_tools/*' WebSocket "
                "commands. Use the dedicated ha-mcp tools instead.",
                parameter="ws_command",
            )
        )
    command_params = (
        _parse_json_dict_param(
            data, type_error_message="ws_command data must be a JSON object"
        )
        or {}
    )
    reserved = _WS_RESERVED_ENVELOPE_KEYS & command_params.keys()
    if reserved:
        raise_failure(
            create_validation_error(
                "data must not contain the reserved WebSocket envelope key(s) "
                f"{', '.join(sorted(reserved))}; the command type is set by "
                "ws_command, and the message id is managed by the transport.",
                parameter="data",
            )
        )
    if refusal := raw_ws_refusal(command_type, command_params):
        raise_failure(
            create_validation_error(
                refusal.message, parameter="ws_command", suggestions=refusal.suggestions
            )
        )
    return command_type, command_params
