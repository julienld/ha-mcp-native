"""
Configuration management tools for Home Assistant Lovelace dashboards.

This module provides tools for managing dashboard metadata and content.
"""

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal, NoReturn, cast, overload

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import ToolResult, tool
from ha_mcp.backends.legacy import legacy_errors, legacy_errors_sync
from ha_mcp.runtime.dashboard_search import (
    _MAX_CARD_DEPTH,
    _SEARCH_MATCH_CAP,
    _find_cards_matching,
    _SearchCriteria,
    _SearchGaps,
    _unread_dashboards_warning,
    build_search_result,
)
from ha_mcp.runtime.dashboard_tools import DashboardTools as DashboardContractTools
from ha_mcp.runtime.dashboard_values import (
    _attach_dashboard_render_paths,
    _large_dashboard_replacement_warning,
    apply_dashboard_python_transform,
    edit_dashboard_result,
    get_dashboard_result,
    scoped_view_get_result,
    set_dashboard_result,
    validate_strategy_dashboard_replacement,
)

from ..client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandNotSent,
    HomeAssistantCommandTimeout,
)
from ..client.websocket_client import get_websocket_client
from ..dashboard_screenshot.capture import (
    DEFAULT_HEIGHT,
    DEFAULT_RENDER_TIMEOUT_SECONDS,
    DEFAULT_WAIT_MS,
    DEFAULT_WIDTH,
    Orientation,
    ScreenshotFormat,
    ViewportPreset,
)
from ..dashboard_screenshot.content import (
    dashboard_image_content,
    dashboard_screenshot_metadata,
    dashboard_screenshot_warnings,
)
from ..dashboard_screenshot.paths import (
    dashboard_frontend_path,
    resolve_dashboard_view,
)
from ..errors import ErrorCode, create_error_response, get_error_code, get_error_message
from ..strict_bps import BestPracticeKeyParam
from ..utils.config_hash import compute_config_hash
from ..utils.dashboard_patch import apply_dashboard_patch
from .auto_backup import with_auto_backup
from .coercion import parse_json_param
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .component_dashboard_edit import edit_dashboard_via_component
from .config_write_helpers import (
    attach_skill_content,
    augment_error_dict_with_skill_content,
    augment_tool_error_with_skill_content,
)
from .dashboard_card_describe import describe_card_response
from .dashboard_edit_errors import (
    raise_dashboard_edit_error,
    raise_dashboard_edit_fetch_error,
    raise_known_dashboard_save_rejection,
)
from .dashboard_list_checks import (
    patch_writes_inside_card,
    reject_malformed_dashboard_config,
    reject_malformed_dashboard_lists,
    reject_malformed_dashboard_patch,
)
from .helpers import (
    exception_to_structured_error,
    extract_tool_error_message,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
    ws_failure_code,
)
from .tool_hints import read_only_hints, write_hints

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _DashboardScreenshotOptions:
    """Shared render options for get/set screenshot paths."""

    view_path: str | None = None
    width: int = DEFAULT_WIDTH
    height: int | Literal["auto"] = DEFAULT_HEIGHT
    viewport_presets: list[ViewportPreset] | None = None
    orientation: Orientation | None = None
    zoom: float = 1.0
    wait_ms: int = DEFAULT_WAIT_MS
    full_page: bool = False
    theme: str | None = None
    dark_mode: bool = False
    language: str | None = None
    image_format: ScreenshotFormat = "png"
    render_timeout_seconds: float = DEFAULT_RENDER_TIMEOUT_SECONDS


# Live card fields replace the static reference only on capable components.
_DASHBOARD_SKILL_FILES: tuple[str, ...] = ("references/dashboard-guide.md",)


async def _attach_dashboard_skill(
    response: dict[str, Any], MandatoryBPS: bool, client: Any
) -> None:
    """In-place attach skill_content to a dashboard response when applicable.

    Delegates to the shared :func:`attach_skill_content` so the
    missing-vendor-warning path is consistent across every write tool.
    """
    files = _DASHBOARD_SKILL_FILES
    if MandatoryBPS and not component_supports(
        await get_component_caps(client), "dashboard_cards"
    ):
        files += ("references/dashboard-cards.md",)
    attach_skill_content(
        response,
        MandatoryBPS=MandatoryBPS,
        canonical_files=files,
        referenced_files=None,
    )


async def _get_dashboard_config_internal(
    client: Any, url_path: str | None
) -> tuple[dict[str, Any], str]:
    """Fetch dashboard config from HA and compute its hash.

    Returns ``(config, config_hash)`` tuple where ``config`` is the
    authoritative Lovelace config dict returned by HA's ``lovelace/config``
    WebSocket call (with ``force=True`` to bypass any cache) and
    ``config_hash`` is computed from that config via ``compute_config_hash``.

    Used internally to obtain the authoritative post-save hash and as the
    fetch+hash building block for the optimistic-locking pre-read paths.
    Mirrors the ``_get_<entity>_config_internal`` helpers in the sibling
    files (``tools_config_scripts.py``, ``tools_config_automations.py``,
    ``tools_config_scenes.py``).

    Raises ``ToolError`` with ``ErrorCode.SERVICE_CALL_FAILED`` if the
    WebSocket call reports failure or the response is not a dict. HA's
    upstream error code is preserved as ``ha_error_code`` when present, so
    callers can distinguish confirmed absence from an unverifiable read.
    """
    get_data: dict[str, Any] = {"type": "lovelace/config", "force": True}
    # No url_path is Core's default: its ``lovelace`` item, else the default store.
    if url_path and url_path != "lovelace":
        get_data["url_path"] = url_path

    response = await client.send_websocket_message(get_data)

    if isinstance(response, dict) and not response.get("success", True):
        error_msg = get_error_message(response)
        if error_msg is None:
            error_msg = str(response.get("error", {}))
        ha_error_code = response.get("error_code") or get_error_code(response)
        error_context: dict[str, Any] = {"url_path": url_path}
        if ha_error_code is not None:
            # Preserve HA's code so downstream safety decisions do not have to
            # infer an absent config from a multiply-prefixed message alone.
            error_context["ha_error_code"] = str(ha_error_code)
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Dashboard fetch failed: {error_msg}",
                context=error_context,
            )
        )

    config = response.get("result") if isinstance(response, dict) else response
    if not isinstance(config, dict):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Dashboard config response was not a dict",
                context={"url_path": url_path},
            )
        )

    return cast(dict[str, Any], config), compute_config_hash(config)


def _lovelace_url_path(url_path: str | None) -> str | None:
    """The ``url_path`` HA's Lovelace reads take: ``None`` for the default dashboard."""
    return None if not url_path or url_path == "default" else url_path


# Card slots (issue #1599) are a discovery heuristic, not an HA contract: HA has
# no nested-card map, and each card decides what its keys mean. These are the
# keys HA's stack/conditional/entity-filter cards and common custom cards use,
# recognised at any depth (``groups[].cards[].card``; #2694): ``cards`` (list),
# ``card`` (dict or list), and ``custom_fields`` / ``states`` (name -> card
# map). A typed dict in a slot is walked as a card; anything else, a typed dict
# elsewhere or a slot key in another shape, is the owning card's option:
# ``query`` reads its text, but entity_id/card_type/heading do not match it.
# Keys holding non-card children (picture-elements ``elements``). Their text is
# read by ``query``, but entity_id/card_type/heading match cards only, so a card
# carrying one is disclosed by its *presence*, not by the absence of matches
# (issue #1599).
# Substring in WS error message that signals the dashboard identifier was not
# accepted by lovelace/config (e.g., caller passed an internal id where url_path
# is expected). Used to gate the lazy resolver fallback in get/set tools.
#
# Source: homeassistant/components/lovelace/websocket.py, _handle_errors —
# emits f"Unknown config specified: {url_path}" paired with structured
# error.code "config_not_found". The client envelope does surface that code
# top-level (``error_code``), but HA reuses it for the no-stored-config case
# ("No config found." — an auto-generated dashboard), so the code alone cannot
# identify an unresolved identifier; the message substring remains the only
# discriminating signal. If HA reformats this string, the lazy fallback
# regresses silently to never firing — re-verify with major HA upgrades.
_LAZY_RESOLVE_TRIGGER = "Unknown config specified"

# Exact ``lovelace/config`` messages meaning that a storage dashboard exists
# but has not been given a config yet. Mirrors smart_search/_deep.py and stays
# deliberately narrower than ``_LAZY_RESOLVE_TRIGGER``: an unknown path or any
# other pre-read failure must remain fail-closed before a full replacement.
_NO_STORED_CONFIG_MESSAGES = frozenset(
    {"No config found.", "Command failed: No config found."}
)
_DASHBOARD_FETCH_FAILED_PREFIX = "Dashboard fetch failed: "
_HA_CONFIG_NOT_FOUND_CODE = "config_not_found"


def _should_lazy_resolve(error_msg: str) -> bool:
    """Return True if a WS error message indicates the identifier needs resolving."""
    return _LAZY_RESOLVE_TRIGGER in error_msg


def _is_no_stored_dashboard_config_error(exc: ToolError) -> bool:
    """Return whether HA confirms that an existing dashboard has no config."""
    try:
        error_data = json.loads(str(exc))
    except (json.JSONDecodeError, TypeError):
        return False
    if (
        not isinstance(error_data, dict)
        or error_data.get("ha_error_code") != _HA_CONFIG_NOT_FOUND_CODE
    ):
        return False
    message = extract_tool_error_message(exc).removeprefix(
        _DASHBOARD_FETCH_FAILED_PREFIX
    )
    return message in _NO_STORED_CONFIG_MESSAGES


# The ha_mcp_tools/dashboards WS command: list / get / search over the live
# lovelace collection in one in-process frame. Named once so the routing helpers
# and their tests agree on the wire string (Global-Constraint-2 idiom, mirroring
# ``component_devices.WS_DEVICE_GET``).
WS_DASHBOARDS = "ha_mcp_tools/dashboards"

# ``LovelaceConfig.mode`` wire string for a storage dashboard. On both paths the
# ``list`` rows normally carry ``mode``: the ha_mcp_tools component tags every
# runtime row, and the legacy ``lovelace/dashboards/list`` rows are the
# ``LovelaceConfig.config`` dicts, where core's schemas stamp it (storage items
# default ``mode: storage`` in ``STORAGE_DASHBOARD_CREATE_FIELDS``; YAML entries
# REQUIRE ``mode: yaml`` in ``YAML_DASHBOARD_SCHEMA`` — verified against
# home-assistant/core ``lovelace/{dashboard,const,__init__}.py``). A YAML
# dashboard's BODY may carry resolved ``!secret`` plaintext, so the
# cross-dashboard ``search`` walk reads a row's body ONLY when it is EXPLICITLY
# tagged ``storage`` — fail-closed, which also skips the rare UNTAGGED row
# (storage items persisted before core's mode default existed) rather than risk
# reading a body it can't prove is storage. ``list`` still surfaces YAML rows,
# since listing metadata is safe.
_DASHBOARD_STORAGE_MODE = "storage"

# Card search match cap, so one response stays bounded.


async def _dashboards_via_component(
    client: Any,
    mode: Literal["list", "get", "search", "docs"],
    *,
    url_path: str | None = None,
    query: str | None = None,
) -> dict[str, Any] | None:
    """One ``ha_mcp_tools/dashboards`` read; ``None`` ⇒ use the legacy path.

    Global-Constraint-2 idiom (mirrors
    ``component_devices.fetch_device_via_component``). Returns the component's
    ``result`` dict for ``mode`` (``list`` / ``get`` / ``search`` / ``docs``) when the
    component advertises the ``dashboards`` capability AND reports
    ``available: true`` (the lovelace integration is set up). Returns ``None`` —
    the caller runs its unchanged legacy path — on capability miss, downgrade
    (``unknown_command`` → invalidate the cached caps), command error/timeout
    (logged), a connection-establishment failure (logged), a malformed
    envelope, or ``available: false`` (lovelace not set up). A
    ``HomeAssistantConnectionError`` — a pooled-WS drop, or a failed
    (re)connect — is caught here and mapped to ``None``: the legacy dashboards
    path rides the
    ``send_websocket_message`` bridge (and the auto-backup capture consumer
    forbids a blocked write), so a wedged component read must fall back to
    legacy rather than escape into a set/delete write path. If the transport
    itself is dead the legacy bridge raises in turn (#1947) and the write
    fails loud, which is the correct outcome for a write that cannot be
    confirmed.
    """
    caps = await get_component_caps(client)
    if not component_supports(caps, "dashboards"):
        return None
    kwargs: dict[str, Any] = {"mode": mode}
    if url_path is not None:
        kwargs["url_path"] = url_path
    if query is not None:
        kwargs["query"] = query
    try:
        ws = await get_websocket_client(
            url=client.base_url,
            token=client.token,
            verify_ssl=getattr(client, "verify_ssl", None),
        )
        raw = await ws.send_command(WS_DASHBOARDS, **kwargs)
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning("%s failed; fell back to legacy: %r", WS_DASHBOARDS, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        # HomeAssistantConnectionError: a pooled-WS drop or a failed
        # (re)connect. Fall back to the
        # legacy bridge rather than escape here; if the transport is genuinely
        # dead the bridge raises there instead (#1947).
        logger.warning(
            "%s connection error; falling back to legacy: %r", WS_DASHBOARDS, exc
        )
        return None
    result = raw.get("result")
    if not isinstance(result, dict) or not result.get("available"):
        logger.debug(
            "%s (mode=%s) returned unavailable/malformed result; falling back to legacy",
            WS_DASHBOARDS,
            mode,
        )
        return None
    return result


async def _component_dashboard_rows(client: Any) -> list[dict[str, Any]] | None:
    """All dashboard metadata rows via the component ``list``; ``None`` ⇒ legacy.

    The legacy ``lovelace/dashboards/list`` returns a metadata row for every
    dashboard that has a config — YAML dashboards included — so the component
    ``list`` is passed through with YAML rows KEPT. Dropping them diverged the two
    paths (a YAML dashboard vanished from ``list_only`` output only when the
    component was installed). Listing metadata is safe: only a dashboard's BODY can
    carry resolved ``!secret`` plaintext, and the body-serving paths still exclude
    YAML — ``get`` via the component's ``yaml_excluded`` status and ``search`` by
    reading only ``mode == "storage"`` rows. The additive ``mode`` tag is preserved
    on every row so those exclusions can key off it. ``None`` (component unavailable
    / malformed) routes the caller to the legacy list read.
    """
    result = await _dashboards_via_component(client, "list")
    if result is None:
        return None
    rows = result.get("dashboards")
    if not isinstance(rows, list):
        logger.debug(
            "%s list returned a non-list 'dashboards' (%s); falling back to legacy",
            WS_DASHBOARDS,
            type(rows).__name__,
        )
        return None
    return [row for row in rows if isinstance(row, dict)]


async def _component_dashboard_config(
    client: Any, url_path: str | None
) -> dict[str, Any] | None:
    """One dashboard's config via the component ``get``; ``None`` ⇒ legacy.

    Returns the config body ONLY when the component is available and reports
    ``status == "ok"``. ``yaml_excluded`` (a YAML body may carry resolved
    ``!secret`` plaintext — the legacy read is authoritative for it) and
    ``not_found`` (let the legacy path produce the real error or lazy-resolve an
    internal id) both return ``None`` so the caller runs its unchanged legacy
    read. ``url_path`` ``None`` targets the default dashboard.
    """
    result = await _dashboards_via_component(client, "get", url_path=url_path)
    if result is None or result.get("status") != "ok":
        return None
    config = result.get("config")
    if not isinstance(config, dict):
        logger.debug(
            "%s get returned status=ok but a non-dict config for url_path=%r; "
            "falling back to legacy",
            WS_DASHBOARDS,
            url_path,
        )
        return None
    return config


async def fetch_dashboards_list(
    client: Any,
) -> list[dict[str, Any]] | None:
    """Fetch and normalise the lovelace/dashboards/list WebSocket response.

    Returns the list of dashboard registry entries on success, or ``None``
    when the response shape is unrecognised.  A warning is logged on
    unexpected shapes so that future HA response-format changes surface at
    every fetch site rather than silently degrading.

    When the ``ha_mcp_tools`` component advertises the ``dashboards`` capability
    the rows — YAML rows included, tagged with ``mode`` (see
    ``_component_dashboard_rows``) — are served from one in-process ``list`` frame (the
    ``_resolve_dashboard`` / ``_lookup_existing_dashboards`` / list-mode callers
    all funnel here); otherwise the legacy ``lovelace/dashboards/list`` WS read
    runs unchanged.

    Callers decide how to handle ``None`` (e.g. fall through to ``[]`` or
    propagate the failure).
    """
    component_rows = await _component_dashboard_rows(client)
    if component_rows is not None:
        return component_rows

    result = await client.send_websocket_message({"type": "lovelace/dashboards/list"})
    if isinstance(result, dict) and isinstance(result.get("result"), list):
        return cast(list[dict[str, Any]], result["result"])
    if isinstance(result, list):
        return cast(list[dict[str, Any]], result)
    logger.warning(
        "lovelace/dashboards/list returned an unexpected shape (type=%s); "
        "treating as no-match",
        type(result).__name__,
    )
    return None


def _raise_dashboard_registry_read_error(*, action: str, url_path: str) -> NoReturn:
    """Raise the shared user-facing error for an unreadable dashboard registry."""
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            "Failed to read the Home Assistant dashboard registry",
            suggestions=[
                "Retry the operation",
                "Check the Home Assistant connection and logs",
                "Use ha_config_get_dashboard(list_only=True) to verify registry access",
            ],
            context={"action": action, "url_path": url_path},
        )
    )


_DASHBOARD_LIST_SUGGESTION = (
    "Use ha_config_get_dashboard(list_only=True) to see available dashboards"
)


def _raise_unresolved_dashboard_delete(
    url_path: str, dashboards: list[dict[str, Any]]
) -> NoReturn:
    """Raise the delete error for an identifier with no collection item."""
    # YAML rows carry no collection id, so ``_resolve_dashboard`` skips them.
    if any(
        d.get("url_path") == url_path and d.get("mode") == "yaml" for d in dashboards
    ):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Dashboard '{url_path}' is a YAML-mode dashboard",
                details="It is defined in configuration.yaml; the "
                "Home Assistant API cannot delete it.",
                context={"action": "delete", "url_path": url_path},
            )
        )
    available_ids = [d.get("url_path") for d in dashboards[:10] if d.get("url_path")]
    raise_tool_error(
        create_error_response(
            ErrorCode.RESOURCE_NOT_FOUND,
            f"Dashboard '{url_path}' not found",
            details=f"No dashboard found with URL path or internal ID '{url_path}'.",
            suggestions=[_DASHBOARD_LIST_SUGGESTION],
            context={
                "action": "delete",
                "url_path": url_path,
                "available_dashboard_ids": available_ids,
            },
        )
    )


async def _resolve_dashboard(
    client: Any, identifier: str
) -> tuple[dict[str, str] | None, list[dict[str, Any]] | None]:
    """Resolve a dashboard identifier (url_path or internal id) to both forms.

    Calls ``lovelace/dashboards/list`` and returns a 2-tuple
    ``(match, dashboards)``:

    - ``match`` is ``{"url_path": ..., "id": ...}`` when the identifier
      matches either field on a registry entry that has both fields
      populated; otherwise ``None``.
    - ``dashboards`` is the raw list as returned by HA when the
      response shape is recognised (dict-with-``result`` or bare list);
      ``None`` when the shape was unexpected and a warning was logged.

    Returning ``dashboards`` alongside ``match`` lets callers reuse the
    list for follow-on checks (existence, id lookup) instead of paying
    a second ``lovelace/dashboards/list`` round-trip.

    Three call sites:
    - **Lazy fallback** (``_lazy_resolve_and_retry``): only invoked after
      ``lovelace/config`` rejected the identifier with
      ``_LAZY_RESOLVE_TRIGGER`` — the round-trip is gated by the caller.
      Discards ``dashboards``.
    - **Eager pre-resolve** (``_resolve_set_dashboard_url_path``, called
      from ``ha_config_set_dashboard``): lets callers pass either form;
      gated on a cheap heuristic ("no hyphen, not 'lovelace'") rather than
      an error from HA. Reuses
      ``dashboards`` for the existence-check in ``_lookup_existing_dashboards``
      (threaded through as ``pre_fetched_dashboards``).
    - **Delete** (``ha_config_delete_dashboard``): resolves either form
      to the registry id before issuing the delete; ``dashboards`` names
      a YAML-mode match or the available paths on a miss.
    """
    dashboards = await fetch_dashboards_list(client)
    if dashboards is None:
        return None, None

    for d in dashboards:
        if d.get("id") == identifier or d.get("url_path") == identifier:
            url_path = d.get("url_path") or ""
            entry_id = d.get("id") or ""
            if not url_path or not entry_id:
                # Malformed registry entry — neither form is safe to
                # forward. Skip rather than return empty strings that
                # would be silently used by callers (e.g.
                # ``delete_dashboard`` would forward ``resolved_id=""``).
                continue
            return {"url_path": url_path, "id": entry_id}, dashboards
    return None, dashboards


@overload
async def _lazy_resolve_and_retry(
    client: Any,
    url_path: str,
    ws_data: dict[str, Any],
    response: Any,
) -> tuple[str, Any]:
    pass


@overload
async def _lazy_resolve_and_retry(
    client: Any,
    url_path: None,
    ws_data: dict[str, Any],
    response: Any,
) -> tuple[None, Any]:
    pass


async def _lazy_resolve_and_retry(
    client: Any,
    url_path: str | None,
    ws_data: dict[str, Any],
    response: Any,
) -> tuple[str | None, Any]:
    """Trigger-gated lazy resolve + single retry of a lovelace/config call.

    If `response` indicates HA rejected the identifier with the
    _LAZY_RESOLVE_TRIGGER substring, resolves `url_path` via
    lovelace/dashboards/list and retries the WS call with the canonical
    url_path. Returns the (possibly updated) url_path and the
    (possibly retried) response so the caller can chain naturally:

        url_path, response = await _lazy_resolve_and_retry(
            client, url_path, ws_data, response
        )

    No-op when:
    - the response is not a failure (success=True or non-dict),
    - ``url_path`` is empty,
    - the error message does not contain ``_LAZY_RESOLVE_TRIGGER``
      (the substring miss),
    - the resolver finds no match,
    - or the resolver itself raises (logged at WARNING).

    In every no-op case the original ``response`` is returned unchanged
    so the caller's existing error-handling path runs against the real
    HA error rather than a synthetic "resolver failed" one.

    The caller's `ws_data` dict is never mutated: when a retry is needed,
    a shallow copy is made and the canonical `url_path` written into the
    copy before the retry call.
    """
    if not (isinstance(response, dict) and not response.get("success", True)):
        return url_path, response
    if not url_path:
        return url_path, response

    err = response.get("error", {})
    err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
    if not _should_lazy_resolve(err_msg):
        return url_path, response

    try:
        resolved, _ = await _resolve_dashboard(client, url_path)
    except Exception as resolver_exc:  # noqa: BLE001
        # Resolver itself raised (timeout, network blip, etc.). Don't let
        # this exception escape and replace the original HA error with
        # one about the resolver — fall through with the original
        # response so the caller surfaces the actual "Unknown config
        # specified" error.
        logger.warning(
            "Lazy resolver failed for url_path=%r: %s; "
            "falling through to original error",
            url_path,
            resolver_exc,
        )
        return url_path, response

    if resolved is None or not resolved["url_path"]:
        return url_path, response

    url_path = resolved["url_path"]
    retry_data = dict(ws_data)
    retry_data["url_path"] = url_path
    response = await client.send_websocket_message(retry_data)
    return url_path, response


async def _attach_dashboard_render_paths_after_write(
    client: Any,
    result: dict[str, Any],
    url_path: str,
    fallback_config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Attach authoritative post-write routes without masking a committed write.

    Home Assistant may normalize the submitted dashboard config. Always read it
    back before claiming canonical render paths. The submitted config remains a
    screenshot-targeting fallback only when that readback fails.
    """
    result.update(config_hash=None, write_committed=True, post_write_verified=False)
    try:
        authoritative_config, config_hash = await _get_dashboard_config_internal(
            client, url_path
        )
    except ToolError as exc:
        result.setdefault("warnings", []).append(
            "Canonical render paths unavailable after the dashboard write: "
            f"{extract_tool_error_message(exc)}"
        )
        return fallback_config
    except Exception as exc:
        # The dashboard write already committed. Metadata enrichment must not
        # turn a successful mutation into a reported failure when the follow-up
        # read hits a raw transport or parsing exception.
        logger.warning(
            "Could not fetch canonical render paths after writing %s: %s",
            url_path,
            exc,
            exc_info=True,
        )
        result.setdefault("warnings", []).append(
            f"Canonical render paths unavailable after the dashboard write: {exc}"
        )
        return fallback_config
    result.update(config_hash=config_hash, post_write_verified=True)
    _attach_dashboard_render_paths(result, url_path, authoritative_config)
    return authoritative_config


def _note_screenshot_ignored(
    result: dict[str, Any],
    *,
    include_screenshot: bool,
    full_page: bool = False,
    options: _DashboardScreenshotOptions | None = None,
    mode: str,
) -> None:
    """Warn when get-mode-only options are passed to a mode that ignores them.

    Screenshot options — and ``view_path``, which is a get-mode scoping
    parameter that merely travels inside the options dataclass — are only
    honoured in get mode. In list and search mode they are accepted but
    inapplicable, so surface a ``warnings`` entry rather than dropping the
    request as a silent no-op (matches the warn-don't-fail contract the
    params document)."""
    capture_options = options or _DashboardScreenshotOptions(full_page=full_page)
    if include_screenshot or capture_options != _DashboardScreenshotOptions():
        result.setdefault("warnings", []).append(
            f"include_screenshot, view_path, and screenshot render options are "
            f"ignored in {mode} mode; call "
            "ha_config_get_dashboard with a url_path (and no search criteria) "
            "to scope to a view or get a screenshot."
        )


async def _capture_dashboard_screenshot_result(
    result: dict[str, Any],
    url_path: str | None,
    *,
    client: Any | None,
    config: dict[str, Any] | None,
    options: _DashboardScreenshotOptions,
) -> ToolResult:
    """Render configured captures and attach their ordered MCP metadata."""
    from ..dashboard_screenshot import capture as screenshot_capture

    render_path = dashboard_frontend_path(url_path)
    if options.view_path is not None or config is not None:
        if config is None:
            if client is None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.INTERNAL_ERROR,
                        "Dashboard client is unavailable for view_path resolution.",
                    )
                )
            config, _ = await _get_dashboard_config_internal(client, url_path)
        target = resolve_dashboard_view(
            url_path or "default", config, options.view_path
        )
        render_path = target.render_path
        if target.warnings:
            result.setdefault("warnings", []).extend(target.warnings)

    capture_failures: list[dict[str, Any]] = []
    guard_warnings: list[str] = []
    captures = await screenshot_capture.capture_dashboard_images(
        render_path,
        width=options.width,
        height=options.height,
        viewport_presets=options.viewport_presets,
        orientation=options.orientation,
        zoom=options.zoom,
        wait_ms=options.wait_ms,
        full_page=options.full_page,
        theme=options.theme,
        dark_mode=options.dark_mode,
        language=options.language,
        image_format=options.image_format,
        render_timeout_seconds=options.render_timeout_seconds,
        partial_failures=capture_failures,
        client=client,
        capture_warnings=guard_warnings,
    )
    # Build every fallible image/metadata object before publishing screenshot
    # fields. The write path can then degrade serialization failures to a
    # warning without returning content_index entries for images that vanished.
    try:
        image_content = dashboard_image_content(captures)
        screenshot_metadata = dashboard_screenshot_metadata(captures, render_path)
        structured_result = {
            **result,
            "screenshot_render_path": render_path,
            "screenshots": screenshot_metadata,
        }
        if capture_failures:
            structured_result["screenshot_partial"] = True
            structured_result["screenshot_failures"] = capture_failures
        capture_warnings = [
            *dashboard_screenshot_warnings(captures),
            *guard_warnings,
        ]
        if capture_warnings:
            structured_result["warnings"] = [
                *structured_result.get("warnings", []),
                *capture_warnings,
            ]
        return ToolResult(
            content=image_content,
            structured_content=structured_result,
        )
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        error_payload = create_error_response(
            ErrorCode.IMAGE_SERIALIZATION_FAILED,
            "Rendered dashboard images could not be packaged into the MCP response.",
            details=str(exc),
            context={"capture_count": len(captures), "render_path": render_path},
        )
        if guard_warnings:
            # The render already happened, so a theme-guard warning (e.g. a
            # failed restore) must stay visible even when packaging fails.
            error_payload["warnings"] = list(guard_warnings)
        raise_tool_error(error_payload)
    # raise_tool_error is typed -> NoReturn, but CodeQL cannot see that, so it
    # reports py/mixed-returns for the implicit None fall-through past the
    # except block. Keep this terminal statement to suppress the false positive.
    raise AssertionError("unreachable: raise_tool_error always raises")


async def _maybe_attach_screenshot(
    result: dict[str, Any],
    url_path: str | None,
    requested: bool,
    *,
    client: Any | None = None,
    config: dict[str, Any] | None = None,
    options: _DashboardScreenshotOptions | None = None,
    full_page: bool = False,
    raise_on_failure: bool = False,
) -> "dict[str, Any] | ToolResult":
    """Optionally render a dashboard and attach ordered native image blocks.

    Shared by ``ha_config_get_dashboard`` (include_screenshot) and
    ``ha_config_set_dashboard`` (return_screenshot). On success returns a
    FastMCP ``ToolResult`` carrying ``result`` as structured_content plus the
    images as content blocks. Render metadata is added to the existing result
    dict, preserving its structured response contract.

    ``raise_on_failure`` governs what a capture failure does. The set path
    (``return_screenshot``) leaves it False: a screenshot failure must never
    break a write that already committed, so it degrades to a ``warnings``
    entry. The get path (``include_screenshot``) passes True: it does not commit
    a dashboard/config write, and the screenshot is the requested payload, so
    an engine failure propagates as a ToolError (matching the dedicated
    ``ha_get_dashboard_screenshot`` tool) instead of being demoted to a
    warning the caller may never inspect. A disabled feature flag is always a
    warning either way — it is an expected configuration state, not a failure.
    """
    capture_options = options or _DashboardScreenshotOptions(full_page=full_page)
    if options is not None and full_page and not capture_options.full_page:
        capture_options = replace(capture_options, full_page=True)

    if not requested:
        defaults = _DashboardScreenshotOptions()
        if capture_options != defaults:
            result.setdefault("warnings", []).append(
                "Screenshot render options are ignored because no screenshot "
                "was requested "
                "(set include_screenshot / return_screenshot to use it)."
            )
        return result

    from ..config import get_global_settings

    if not get_global_settings().enable_dashboard_screenshot:
        result.setdefault("warnings", []).append(
            "Screenshot requested but dashboard screenshot mode is disabled. "
            "Enable the 'dashboard screenshot' beta feature to use it."
        )
        return result

    try:
        return await _capture_dashboard_screenshot_result(
            result,
            url_path,
            client=client,
            config=config,
            options=capture_options,
        )
    except ToolError as e:
        if raise_on_failure:
            raise
        return _attach_screenshot_tool_error(result, e)
    except Exception as e:
        # On the set path a screenshot failure must never break a write that
        # already committed, so catch everything non-ToolError (lazy import
        # errors, Image construction, timeouts, transport) and degrade to a
        # warning. On the get path (raise_on_failure) there is nothing to
        # protect, so let it surface.
        if raise_on_failure:
            raise
        logger.warning("Dashboard screenshot capture failed: %s", e, exc_info=True)
        result["screenshot_error"] = {
            "code": ErrorCode.INTERNAL_ERROR.value,
            "message": str(e),
        }
        result.setdefault("warnings", []).append(f"Screenshot unavailable: {e}")
        return result


def _attach_screenshot_tool_error(
    result: dict[str, Any], error: ToolError
) -> dict[str, Any]:
    """Preserve a structured capture failure on an already-committed write."""
    try:
        error_payload = json.loads(str(error))
    except (json.JSONDecodeError, TypeError):
        error_payload = {}
    if not isinstance(error_payload, dict):
        error_payload = {}
    structured_error = error_payload.get("error", {})
    if not isinstance(structured_error, dict):
        structured_error = {}
    screenshot_error: dict[str, Any] = {
        "code": structured_error.get("code", ErrorCode.INTERNAL_ERROR.value),
        "message": structured_error.get("message", str(error)),
    }
    screenshot_error.update(
        {
            key: value
            for key, value in error_payload.items()
            if key not in {"success", "error", "warnings"}
        }
    )
    result["screenshot_error"] = screenshot_error
    result.setdefault("warnings", []).append(
        f"Screenshot unavailable: {extract_tool_error_message(error)}"
    )
    # Theme-guard warnings ride on the error payload (a failing batch may
    # already have rendered and clobbered the engine user's theme); keep them
    # visible on the degraded-to-warning path instead of burying them in
    # screenshot_error.
    payload_warnings = error_payload.get("warnings")
    if isinstance(payload_warnings, list):
        result["warnings"].extend(str(warning) for warning in payload_warnings)
    return result


class LegacyDashboards:
    """Home Assistant dashboard configuration tools."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def get(
        self,
        url_path: str | None = None,
        list_only: bool = False,
        force_reload: bool = False,
        entity_id: str | None = None,
        card_type: str | None = None,
        heading: str | None = None,
        include_config: bool = False,
        include_screenshot: bool = False,
        view_path: str | None = None,
        query: str | None = None,
        describe: bool = False,
    ) -> Any:
        if describe:
            return await describe_card_response(self._client, card_type)
        screenshot_options = _DashboardScreenshotOptions(view_path=view_path)
        criteria = _SearchCriteria(
            entity_id=entity_id, card_type=card_type, heading=heading, query=query
        )
        # The raw parameters, not criteria.is_empty(): a blank-only search must
        # reach its "Search needs ..." error instead of falling through to a get.
        search_mode = any(v is not None for v in (query, entity_id, card_type, heading))
        # Mutable single-element holder so the mode helpers can surface the
        # lazy-resolved/canonicalized url_path back to this scope even when
        # they raise an unexpected (non-ToolError) exception instead of
        # returning normally — the outer except block below needs it for
        # accurate error context.
        resolved_url_path: list[str | None] = [url_path]
        try:
            if list_only and not search_mode:
                return await self._get_dashboard_list_mode(
                    include_screenshot=include_screenshot,
                    screenshot_options=screenshot_options,
                )

            # ``url_path`` is optional in this tool (omitted with
            # ``list_only=True`` lists all dashboards — handled above; omitted
            # without ``list_only`` falls back to the default dashboard via
            # the resolver below). When provided, reject empty/whitespace
            # up-front so the caller gets a structured parameter error
            # instead of a misleading ``RESOURCE_NOT_FOUND``. Extension of
            # the #1312 validate_identifier_not_empty pattern to the
            # dashboards family per #1313.
            if url_path is not None:
                validate_identifier_not_empty(
                    url_path,
                    "url_path",
                    suggestions=[
                        "Pass a dashboard URL path (e.g. 'lovelace-home')",
                        "Omit url_path and pass list_only=True to list dashboards",
                        "Use 'default' to target the default dashboard",
                    ],
                )

            if search_mode:
                return await self._get_dashboard_search_mode(
                    url_path,
                    resolved_url_path=resolved_url_path,
                    criteria=criteria,
                    include_config=include_config,
                    list_only=list_only,
                    include_screenshot=include_screenshot,
                    screenshot_options=screenshot_options,
                )

            return await self._get_dashboard_get_mode(
                url_path,
                resolved_url_path=resolved_url_path,
                force_reload=force_reload,
                view_path=view_path,
                include_screenshot=include_screenshot,
                screenshot_options=screenshot_options,
            )
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            effective_url_path = resolved_url_path[0]
            context: dict[str, Any]
            if search_mode:
                suggestions = [
                    "Check HA connection",
                    "Verify dashboard with ha_config_get_dashboard(list_only=True)",
                ]
                context = {
                    "action": "search",
                    "url_path": effective_url_path,
                    **asdict(criteria),
                }
            else:
                suggestions = [
                    "Use ha_config_get_dashboard(list_only=True) to see available dashboards",
                    "Check if you have permission to access this dashboard",
                    "Use url_path='default' for default dashboard",
                ]
                context = {
                    "action": "get" if not list_only else "list",
                    "url_path": effective_url_path,
                }
            exception_to_structured_error(
                e,
                context=context,
                suggestions=suggestions,
            )
            return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    async def _get_dashboard_list_mode(
        self,
        *,
        include_screenshot: bool,
        screenshot_options: _DashboardScreenshotOptions,
    ) -> dict[str, Any]:
        """``list_only=True`` mode: list every dashboard's metadata row.

        Storage and YAML dashboards alike (metadata only — no bodies — so a
        YAML dashboard's resolved ``!secret`` never surfaces here), matching the
        legacy ``lovelace/dashboards/list`` row set.
        """
        dashboards = await fetch_dashboards_list(self._client) or []
        list_result: dict[str, Any] = {
            "success": True,
            "action": "list",
            "dashboards": dashboards,
            "count": len(dashboards),
        }
        _note_screenshot_ignored(
            list_result,
            include_screenshot=include_screenshot,
            options=screenshot_options,
            mode="list",
        )
        return list_result

    async def _fetch_search_dashboard_config(
        self, url_path: str
    ) -> tuple[dict[str, Any], str, str | None]:
        """Fetch + resolve the dashboard config for search mode.

        Returns ``(config, url_path, search_resolved_from)`` — ``url_path`` is
        the canonicalized identifier (post lazy-resolve) and
        ``search_resolved_from`` is the original caller-passed identifier when
        it differed from the canonical form, else ``None``.
        """
        get_data: dict[str, Any] = {"type": "lovelace/config", "force": True}
        lovelace_url_path = _lovelace_url_path(url_path)
        if lovelace_url_path is not None:
            get_data["url_path"] = lovelace_url_path

        response = await self._client.send_websocket_message(get_data)

        # Lazy resolver fallback: same gate as get-mode. If the caller passed
        # an internal id where url_path is expected, HA rejects with the
        # trigger substring; resolve and retry once. (set_dashboard handles
        # this via an eager pre-resolver, so it has no equivalent fallback
        # here.)
        search_resolved_from: str | None = None
        if lovelace_url_path is not None:
            new_url_path, response = await _lazy_resolve_and_retry(
                self._client, lovelace_url_path, get_data, response
            )
            if new_url_path != lovelace_url_path:
                # Surface the original caller-passed identifier so the
                # caller can see their input was canonicalized.
                search_resolved_from = url_path
                url_path = new_url_path

        if isinstance(response, dict) and not response.get("success", True):
            error_msg = response.get("error", {})
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", str(error_msg))
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to get dashboard: {error_msg}",
                    suggestions=[
                        "Verify dashboard exists with ha_config_get_dashboard(list_only=True)",
                        "Check HA connection",
                    ],
                    context={"action": "search", "url_path": url_path},
                )
            )

        config = response.get("result") if isinstance(response, dict) else response
        if not isinstance(config, dict):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Dashboard config is empty or invalid",
                    suggestions=["Initialize dashboard with ha_config_set_dashboard"],
                    context={"action": "search", "url_path": url_path},
                )
            )

        if "strategy" in config:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_FAILED,
                    "Strategy dashboards have no explicit cards to search",
                    suggestions=[
                        "Use 'Take Control' in HA UI to convert to editable",
                        "Or create a non-strategy dashboard",
                    ],
                    context={"action": "search", "url_path": url_path},
                )
            )

        return config, url_path, search_resolved_from

    _build_search_result = staticmethod(build_search_result)

    async def _get_dashboard_search_mode(
        self,
        url_path: str | None,
        *,
        resolved_url_path: list[str | None],
        criteria: _SearchCriteria,
        include_config: bool,
        list_only: bool,
        include_screenshot: bool,
        screenshot_options: _DashboardScreenshotOptions,
    ) -> dict[str, Any]:
        """Search cards, badges and header cards in one dashboard or all of them.

        With ``url_path`` the dashboard is read directly (YAML included, with
        bodies withheld). Without it every storage dashboard is searched: the
        component serves all their configs in one frame, and otherwise each
        storage dashboard HA lists is read (never the default, which HA does
        not list).
        """
        if not criteria.can_match_cards() and not criteria.can_match_badges():
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "card_type='badge' cannot be combined with heading: view badges "
                    "have no heading, so the search could never match",
                    suggestions=[
                        "Drop heading to list badges, or drop card_type='badge' to "
                        "search cards by heading",
                    ],
                    context={"action": "search"},
                )
            )
        if criteria.is_empty():
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Search needs a non-blank query, entity_id, card_type or heading",
                    suggestions=[
                        "Pass query='light.kitchen' to find cards mentioning it",
                        "Use list_only=True to list dashboards, or url_path=... to get one",
                    ],
                    context={"action": "search"},
                )
            )

        notes: list[str] = []
        if url_path is None:
            docs, notes = await self._search_docs()
            search_result = self._build_search_result(
                docs, criteria=criteria, include_config=include_config
            )
        else:
            (
                config,
                url_path,
                search_resolved_from,
            ) = await self._fetch_search_dashboard_config(url_path)
            # Surface the canonicalized url_path to the caller's scope now, so
            # an unexpected exception below still reports the resolved
            # identifier (see ha_config_get_dashboard's except block).
            resolved_url_path[0] = url_path
            # Fail-closed storage guard: bodies and query-matched values are
            # surfaced ONLY for a dashboard PROVABLY tagged mode="storage". A
            # YAML or unconfirmed (untagged, default) dashboard may carry
            # resolved !secret plaintext. Only paid when either was asked for.
            config_suppressed_note: str | None = None
            if (
                include_config or criteria.query is not None
            ) and not await self._dashboard_is_storage_mode(url_path):
                config_suppressed_note = (
                    "Matched values and card config bodies were withheld: "
                    f"dashboard {url_path!r} is not provably storage-mode (a YAML "
                    "dashboard's config can carry resolved !secret values). Match "
                    "locations and field names are reported."
                )
            search_result = self._build_search_result(
                [{"url_path": url_path, "config": config}],
                criteria=criteria,
                include_config=include_config,
                url_path=url_path,
                config_suppressed_note=config_suppressed_note,
            )
            if search_resolved_from is not None:
                search_result["resolved_from"] = search_resolved_from
        if list_only:
            notes.append("list_only was ignored because search parameters were given.")
        if notes:
            search_result.setdefault("warnings", []).extend(notes)
        _note_screenshot_ignored(
            search_result,
            include_screenshot=include_screenshot,
            options=screenshot_options,
            mode="search",
        )
        return search_result

    async def _search_docs(self) -> tuple[list[dict[str, Any]], list[str]]:
        """Every storage dashboard's ``{url_path, config}`` and warnings about gaps.

        One component frame when it advertises ``dashboards_docs``; otherwise, or
        when that frame is unavailable, one read per storage dashboard HA lists,
        which never includes the default.
        """
        caps = await get_component_caps(self._client)
        if component_supports(caps, "dashboards_docs"):
            result = await _dashboards_via_component(self._client, "docs")
            if result is not None and isinstance(result.get("docs"), list):
                failed = int(result.get("load_failed", 0) or 0)
                return result["docs"], _unread_dashboards_warning(failed)
        docs, unread = await self._collect_legacy_search_docs()
        return docs, [
            *unread,
            "The default dashboard was not searched: only the dashboard read of a "
            "current ha_mcp_tools component reaches it, and that read was not "
            "available. Search it with url_path='default'.",
        ]

    async def _collect_legacy_search_docs(
        self,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Storage dashboards' ``{url_path, config}`` and warnings about unread ones.

        One ``lovelace/config`` read per dashboard ``fetch_dashboards_list``
        returns. A body is read ONLY when its row is EXPLICITLY tagged
        ``mode == "storage"`` — fail-closed: HA resolves ``!secret`` when it
        loads a YAML Lovelace config. Core stamps ``mode`` on both kinds of row,
        so beyond YAML rows this skips only an untagged row (a storage item
        persisted before core's mode default existed). A dashboard with no
        stored config yet has nothing to search; any other read failure is
        reported, not hidden.
        """
        rows = await fetch_dashboards_list(self._client)
        if rows is None:
            return [], [
                "The dashboards list could not be read, so no stored dashboard "
                "was searched."
            ]
        docs: list[dict[str, Any]] = []
        failed: list[str] = []
        for row in rows:
            url_path = row.get("url_path")
            if not url_path or row.get("mode") != _DASHBOARD_STORAGE_MODE:
                continue
            try:
                config, _config_hash = await _get_dashboard_config_internal(
                    self._client, url_path
                )
            except ToolError as exc:
                if not _is_no_stored_dashboard_config_error(exc):
                    logger.warning(
                        "Card search could not read dashboard %r: %s", url_path, exc
                    )
                    failed.append(url_path)
                continue
            docs.append({"url_path": url_path, "config": config})
        return docs, _unread_dashboards_warning(failed)

    async def _dashboard_is_storage_mode(self, url_path: str | None) -> bool:
        """True only when ``url_path`` is a dashboard PROVABLY tagged mode="storage".

        Fail-closed, like the storage check of ``_collect_legacy_search_docs``:
        the dashboards-list row for ``url_path`` must carry an explicit
        ``mode == "storage"``. A YAML row, an untagged row, or a dashboard absent
        from the list (the default dashboard is never listed) all return ``False``
        — its config body may carry HA-resolved ``!secret`` plaintext and must not
        be surfaced. A malformed/unavailable list also fails closed.
        """
        rows = await fetch_dashboards_list(self._client) or []
        for row in rows:
            if row.get("url_path") == url_path:
                return row.get("mode") == _DASHBOARD_STORAGE_MODE
        return False

    async def _get_dashboard_get_mode(
        self,
        url_path: str | None,
        *,
        resolved_url_path: list[str | None],
        force_reload: bool,
        view_path: str | None,
        include_screenshot: bool,
        screenshot_options: _DashboardScreenshotOptions,
    ) -> "dict[str, Any] | ToolResult":
        """Get mode: return the full Lovelace config for a single dashboard.

        The component ``get`` serves this from one in-process, freshness-safe read
        (the default dashboard included — the tool's ``"default"``/omitted alias
        maps to the component's ``None`` url_path); ``None`` falls back to the
        unchanged legacy ``lovelace/config`` read, which also covers YAML bodies
        (never served in-process) and internal-id lazy-resolve.

        ``force_reload`` bypasses the component fast path entirely: the component
        ``get`` carries no force semantic, so a forced read must go straight to the
        legacy ``lovelace/config`` request below (which threads ``force=True``) to
        actually bust HA's Lovelace cache.
        """
        lovelace_url_path = _lovelace_url_path(url_path)
        component_config = (
            None
            if force_reload
            else await _component_dashboard_config(self._client, lovelace_url_path)
        )
        if component_config is not None:
            # The component matches an exact url_path (or the default), so no
            # lazy-resolve is possible: original == final, resolved_from unset.
            resolved_url_path[0] = url_path
            return await self._finalize_get_result(
                url_path,
                component_config,
                original_url_path=url_path,
                view_path=view_path,
                include_screenshot=include_screenshot,
                screenshot_options=screenshot_options,
            )

        data: dict[str, Any] = {"type": "lovelace/config", "force": force_reload}
        if lovelace_url_path is not None:
            data["url_path"] = lovelace_url_path

        response = await self._client.send_websocket_message(data)

        # Lazy resolver fallback: if HA rejects the identifier as unknown,
        # resolve it via lovelace/dashboards/list and retry once. The
        # round-trip is only paid when the caller passed an internal
        # dashboard id (or another non-url_path form) HA does not accept.
        original_url_path = url_path
        url_path, response = await _lazy_resolve_and_retry(
            self._client, url_path, data, response
        )
        # Surface the canonicalized url_path to the caller's scope now, so
        # an unexpected exception from the config processing below still
        # reports the resolved identifier (see ha_config_get_dashboard's
        # except block).
        resolved_url_path[0] = url_path

        # Check if request failed (after potential retry)
        if isinstance(response, dict) and not response.get("success", True):
            error_msg = response.get("error", {})
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", str(error_msg))
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    str(error_msg),
                    suggestions=[
                        "Use ha_config_get_dashboard(list_only=True) to see available dashboards",
                        "Check if you have permission to access this dashboard",
                        "Use url_path='default' for default dashboard",
                    ],
                    context={"action": "get", "url_path": url_path},
                )
            )

        # Extract config from WebSocket response
        config = response.get("result") if isinstance(response, dict) else response

        return await self._finalize_get_result(
            url_path,
            config,
            original_url_path=original_url_path,
            view_path=view_path,
            include_screenshot=include_screenshot,
            screenshot_options=screenshot_options,
        )

    async def _finalize_get_result(
        self,
        url_path: str | None,
        config: Any,
        *,
        original_url_path: str | None,
        view_path: str | None,
        include_screenshot: bool,
        screenshot_options: _DashboardScreenshotOptions,
    ) -> "dict[str, Any] | ToolResult":
        """Shape + return the get-mode response for an already-fetched config.

        Shared by the component fast path and the legacy path so both produce a
        byte-identical envelope: ``config_hash`` (optimistic locking), size,
        render paths, ``resolved_from`` (when the legacy lazy resolver
        canonicalised an internal id), the large-config disclosure hint, and an
        optional screenshot.

        ``view_path`` scopes the payload to one view: the response carries
        ``view`` + ``view_index`` instead of ``config``. The scoped envelope
        deliberately has NO ``config`` key so the view object cannot be
        mistaken for a full config and pushed back through
        ``ha_config_set_dashboard(config=...)``, which would drop every other
        view; ``config_hash``/``config_size_bytes`` still describe the full
        config so ``python_transform`` optimistic locking works unchanged.
        """
        # Compute hash for optimistic locking in subsequent operations
        get_result = legacy_errors_sync(get_dashboard_result)(
            url_path, config, original_url_path=original_url_path, view_path=view_path
        )
        return await _maybe_attach_screenshot(
            get_result,
            url_path,
            include_screenshot,
            client=self._client,
            config=config if isinstance(config, dict) else None,
            # view_path doubles as the get-mode scoping parameter now, so a
            # scoped read without a screenshot request must not trip the
            # "screenshot render options are ignored" warning.
            options=(
                screenshot_options
                if include_screenshot
                else replace(screenshot_options, view_path=None)
            ),
            raise_on_failure=True,
        )

    _scoped_view_get_result = staticmethod(legacy_errors_sync(scoped_view_get_result))

    async def set(
        self,
        url_path: str,
        config: dict[str, Any] | None = None,
        python_transform: str | None = None,
        config_hash: str | None = None,
        title: str | None = None,
        icon: str | None = None,
        require_admin: bool | None = None,
        show_in_sidebar: bool | None = None,
        MandatoryBPS: bool = True,
        BestPracticeKey: BestPracticeKeyParam = None,
        return_screenshot: bool = False,
        view_path: str | None = None,
        patch: list[dict[str, Any]] | None = None,
    ) -> Any:
        action = (
            "patch"
            if patch is not None
            else ("python_transform" if python_transform is not None else "set")
        )
        screenshot_options = _DashboardScreenshotOptions(view_path=view_path)
        try:
            # Reject an invalid view_path BEFORE committing the write. On the
            # return_screenshot path a screenshot failure is demoted to a
            # warning after the write commits, so without this a blank view_path
            # would be swallowed as a warning on an already-committed write.
            if return_screenshot and view_path is not None and not view_path.strip():
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "view_path cannot be empty.",
                        context={"action": action, "view_path": view_path},
                    )
                )
            (
                url_path,
                pre_resolved_from,
                pre_fetched_dashboards,
            ) = await self._resolve_set_dashboard_url_path(url_path, action=action)

            if (
                sum(value is not None for value in (config, python_transform, patch))
                > 1
            ):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "config, python_transform and patch are mutually exclusive "
                        "and cannot be used simultaneously",
                        suggestions=[
                            "Use only ONE of: config, python_transform or patch",
                            "config: Full replacement",
                            "python_transform: Loops and pattern-based edits",
                        ],
                        context={"action": action, "url_path": url_path},
                    )
                )

            if patch is not None and any(
                value is not None
                for value in (title, icon, require_admin, show_in_sidebar)
            ):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "patch cannot be combined with dashboard metadata "
                        "(title, icon, require_admin, show_in_sidebar)",
                        suggestions=[
                            "Update metadata in a separate ha_config_set_dashboard call "
                            "without patch, config or python_transform",
                        ],
                        context={
                            "action": action,
                            "url_path": url_path,
                            "write_committed": False,
                        },
                    )
                )

            if patch is not None:
                return await self._run_dashboard_patch(
                    url_path,
                    config_hash,
                    patch,
                    pre_resolved_from,
                    MandatoryBPS,
                    return_screenshot=return_screenshot,
                    screenshot_options=screenshot_options,
                )

            if python_transform is not None:
                return await self._run_dashboard_python_transform(
                    url_path,
                    config_hash,
                    python_transform,
                    pre_resolved_from,
                    MandatoryBPS,
                    return_screenshot=return_screenshot,
                    screenshot_options=screenshot_options,
                )

            return await self._run_dashboard_config_update(
                url_path,
                config,
                config_hash,
                title=title,
                icon=icon,
                require_admin=require_admin,
                show_in_sidebar=show_in_sidebar,
                pre_resolved_from=pre_resolved_from,
                pre_fetched_dashboards=pre_fetched_dashboards,
                return_screenshot=return_screenshot,
                screenshot_options=screenshot_options,
                MandatoryBPS=MandatoryBPS,
            )

        except ToolError as te:
            raise augment_tool_error_with_skill_content(te, bp_warnings=None) from None
        except Exception as e:  # noqa: BLE001
            error = exception_to_structured_error(
                e,
                context={"action": action, "url_path": url_path},
                raise_error=False,
            )
            augment_error_dict_with_skill_content(error, bp_warnings=None)
            raise_tool_error(error)
            return None

    async def _resolve_set_dashboard_url_path(
        self, url_path: str, *, action: str = "set"
    ) -> tuple[str, str | None, list[dict[str, Any]] | None]:
        """Canonicalize supported dashboard identifiers for the set tool.

        Home Assistant itself rejects a new hyphenless ``url_path`` on create;
        this only maps HA-MCP's aliases onto an existing dashboard.

        Returns ``(url_path, pre_resolved_from, pre_fetched_dashboards)``:
        ``url_path`` is canonicalized (``"default"`` -> ``"lovelace"``, and an
        internal-id form pre-resolved to its url_path when it matches an
        existing dashboard). ``pre_resolved_from`` is the original
        caller-passed identifier when the pre-resolver rewrote it, else
        ``None``. ``pre_fetched_dashboards`` is the ``lovelace/dashboards/list``
        response already fetched by the pre-resolver when it fired, so the
        caller can reuse it instead of paying a second round-trip.
        """
        # ``url_path`` is required (always non-None). Reject empty/
        # whitespace up-front so the caller gets a structured parameter
        # error instead of a misleading downstream failure. Extension of
        # the #1312 validate_identifier_not_empty pattern to the dashboards
        # family per #1313.
        validate_identifier_not_empty(
            url_path,
            "url_path",
            suggestions=[
                "Pass a dashboard URL path (e.g. 'my-dashboard')",
                "Use 'default' or 'lovelace' for the default dashboard",
            ],
            context={"action": action},
        )
        # Handle "default" as alias for the default dashboard
        # (matches ha_config_get_dashboard behavior)
        if url_path == "default":
            url_path = "lovelace"

        # A hyphenless identifier may be an internal ID (``my_dash``) of an
        # existing dashboard (``my-dash``). This is still a create-or-update
        # API: ``action`` reports create vs update; ``resolved_from``
        # additionally reports only this rewrite.
        pre_resolved_from: str | None = None
        pre_fetched_dashboards: list[dict[str, Any]] | None = None
        if "-" not in url_path and url_path != "lovelace":
            resolved, dashboards = await _resolve_dashboard(self._client, url_path)
            if dashboards is None:
                _raise_dashboard_registry_read_error(action=action, url_path=url_path)
            pre_fetched_dashboards = dashboards
            if resolved is not None and resolved["url_path"] != url_path:
                pre_resolved_from = url_path
                url_path = resolved["url_path"]
                logger.info(
                    "ha_config_set_dashboard pre-resolver mapped %r -> %r",
                    pre_resolved_from,
                    url_path,
                )

        return url_path, pre_resolved_from, pre_fetched_dashboards

    async def _fetch_and_verify_dashboard_hash(
        self, url_path: str, config_hash: str, *, action: str = "python_transform"
    ) -> dict[str, Any]:
        """Fetch current dashboard config and verify ``config_hash`` (optimistic locking).

        Re-wraps the shared fetch helper's generic error with
        edit-specific UX suggestions, and raises on a hash
        mismatch (concurrent edit since the caller's last read).
        """
        try:
            current_config, current_hash = await _get_dashboard_config_internal(
                self._client, url_path
            )
        except ToolError as e:
            raise_dashboard_edit_fetch_error(e, url_path, action)

        if current_hash != config_hash:
            raise_dashboard_edit_error(
                url_path,
                "conflict",
                "Dashboard modified since last read (conflict)",
                False,
                action,
            )
        return current_config

    _apply_dashboard_python_transform = staticmethod(
        legacy_errors_sync(apply_dashboard_python_transform)
    )

    async def _save_dashboard_python_transform(
        self,
        url_path: str,
        transformed_config: dict[str, Any],
        *,
        action: str = "python_transform",
    ) -> tuple[dict[str, Any], str | None, str | None]:
        """Save transformed config and best-effort reload its authoritative form."""
        await self._save_dashboard_config(url_path, transformed_config, action=action)

        # HA may normalize after save, so prefer an authoritative re-fetch. The
        # mutation has already committed at this point: a follow-up read failure
        # must not make the caller believe the write itself failed.
        try:
            post_save_config, new_config_hash = await _get_dashboard_config_internal(
                self._client, url_path
            )
        except ToolError as exc:
            warning = (
                "Dashboard was updated, but its authoritative post-save config "
                f"could not be reloaded: {extract_tool_error_message(exc)}"
            )
            return transformed_config, None, warning
        except Exception as exc:
            logger.warning(
                "Could not reload dashboard %s after %s: %s",
                url_path,
                action,
                exc,
                exc_info=True,
            )
            warning = (
                "Dashboard was updated, but its authoritative post-save config "
                f"could not be reloaded: {exc}"
            )
            return transformed_config, None, warning
        return post_save_config, new_config_hash, None

    async def _run_dashboard_python_transform(
        self,
        url_path: str,
        config_hash: str | None,
        python_transform: str,
        pre_resolved_from: str | None,
        MandatoryBPS: bool,
        *,
        return_screenshot: bool,
        screenshot_options: _DashboardScreenshotOptions,
    ) -> "dict[str, Any] | ToolResult":
        """Execute python_transform mode and return the tool response."""
        if config_hash is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "config_hash is required for python_transform",
                    suggestions=[
                        "Call ha_config_get_dashboard() first",
                        "Use the config_hash from that response",
                    ],
                    context={"action": "python_transform", "url_path": url_path},
                )
            )

        current_config = await self._fetch_and_verify_dashboard_hash(
            url_path, config_hash
        )
        transformed_config = self._apply_dashboard_python_transform(
            url_path, python_transform, current_config
        )
        native_result = await edit_dashboard_via_component(
            self._client,
            url_path,
            expected_hash=config_hash,
            config=transformed_config,
            action="python_transform",
        )
        if native_result is None:
            native_result = await self._save_dashboard_edit_legacy(
                url_path, transformed_config
            )
        return await self._finish_dashboard_edit(
            url_path,
            native_result,
            "python_transform",
            pre_resolved_from,
            MandatoryBPS,
            python_transform=python_transform,
            return_screenshot=return_screenshot,
            screenshot_options=screenshot_options,
        )

    async def _save_dashboard_edit_legacy(
        self,
        url_path: str,
        config: dict[str, Any],
        *,
        action: str = "python_transform",
    ) -> dict[str, Any]:
        """Adapt the existing save/readback path to the shared result contract."""
        post_config, config_hash, warning = await self._save_dashboard_python_transform(
            url_path, config, action=action
        )
        return {
            "config": post_config,
            "config_hash": config_hash,
            "write_committed": True,
            "post_write_verified": warning is None,
            "warnings": [warning] if warning else [],
        }

    async def _reject_malformed_patched_dashboard(
        self, url_path: str, patch: list[dict[str, Any]]
    ) -> None:
        """Check the patched config when an edit lands inside a card."""
        current, _ = await _get_dashboard_config_internal(self._client, url_path)
        try:
            candidate = apply_dashboard_patch(current, patch)
        except ValueError:
            return  # An unapplicable patch is rejected by the edit itself.
        reject_malformed_dashboard_lists(candidate, url_path, source="patch")

    async def _run_dashboard_patch(
        self,
        url_path: str,
        config_hash: str | None,
        patch: list[dict[str, Any]] | str,
        pre_resolved_from: str | None,
        MandatoryBPS: bool,
        *,
        return_screenshot: bool,
        screenshot_options: _DashboardScreenshotOptions,
    ) -> "dict[str, Any] | ToolResult":
        """Apply structured edits in Core, or use the existing legacy save path."""
        if config_hash is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "config_hash is required for patch",
                    suggestions=["Read the dashboard first and use its config_hash"],
                    context={"action": "patch", "url_path": url_path},
                )
            )
        try:
            parsed_patch = parse_json_param(patch, "patch")
            if not isinstance(parsed_patch, list):
                raise ValueError("patch must be a list of operations")
        except ValueError as exc:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    str(exc),
                    context={"action": "patch", "url_path": url_path},
                )
            )
        reject_malformed_dashboard_patch(parsed_patch, url_path)
        if patch_writes_inside_card(parsed_patch):
            await self._reject_malformed_patched_dashboard(url_path, parsed_patch)
        result = await edit_dashboard_via_component(
            self._client,
            url_path,
            expected_hash=config_hash,
            patch=parsed_patch,
            action="patch",
        )
        if result is None:
            current = await self._fetch_and_verify_dashboard_hash(
                url_path, config_hash, action="patch"
            )

            try:
                updated = apply_dashboard_patch(current, parsed_patch)
            except ValueError as exc:
                raise_dashboard_edit_error(
                    url_path, "validation_failed", str(exc), False, "patch"
                )
            self._validate_strategy_dashboard_replacement(
                url_path,
                was_strategy_dashboard="strategy" in current,
                replacement_config=updated,
                action="patch",
            )
            # Compare JSON hashes rather than Python equality: true and 1 are
            # different dashboard values even though Python considers them equal.
            if compute_config_hash(updated) == config_hash:
                result = {
                    "config": current,
                    "config_hash": config_hash,
                    "write_committed": False,
                    "post_write_verified": True,
                    "unchanged": True,
                }
            else:
                result = await self._save_dashboard_edit_legacy(
                    url_path, updated, action="patch"
                )
        return await self._finish_dashboard_edit(
            url_path,
            result,
            "patch",
            pre_resolved_from,
            MandatoryBPS,
            return_screenshot=return_screenshot,
            screenshot_options=screenshot_options,
        )

    async def _finish_dashboard_edit(
        self,
        url_path: str,
        edit: dict[str, Any],
        action: str,
        pre_resolved_from: str | None,
        MandatoryBPS: bool,
        *,
        return_screenshot: bool,
        screenshot_options: _DashboardScreenshotOptions,
        python_transform: str | None = None,
    ) -> "dict[str, Any] | ToolResult":
        """Preserve public edit fields, authoritative paths, BPS and screenshots."""
        result = legacy_errors_sync(edit_dashboard_result)(
            url_path, edit, action, pre_resolved_from, python_transform=python_transform
        )
        await _attach_dashboard_skill(result, MandatoryBPS, self._client)
        return await _maybe_attach_screenshot(
            result,
            url_path,
            return_screenshot,
            client=self._client,
            config=edit["config"],
            options=screenshot_options,
        )

    async def _lookup_existing_dashboards(
        self, url_path: str, pre_fetched_dashboards: list[dict[str, Any]] | None
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Resolve whether ``url_path`` already exists, reusing a pre-fetched list when available."""
        if pre_fetched_dashboards is not None:
            existing_dashboards = pre_fetched_dashboards
        else:
            fetched_dashboards = await fetch_dashboards_list(self._client)
            if fetched_dashboards is None:
                _raise_dashboard_registry_read_error(action="set", url_path=url_path)
            existing_dashboards = fetched_dashboards
        dashboard_exists = any(
            d.get("url_path") == url_path for d in existing_dashboards
        )
        # The built-in default dashboard ("lovelace") is always present
        # but isn't listed by lovelace/dashboards/list on fresh installs
        if url_path == "lovelace":
            dashboard_exists = True
        return dashboard_exists, existing_dashboards

    async def _create_dashboard(
        self,
        url_path: str,
        *,
        title: str | None,
        icon: str | None,
        require_admin: bool | None,
        show_in_sidebar: bool | None,
    ) -> str | None:
        """Create a new storage-mode dashboard and return its dashboard_id."""
        # Core's create schema supplies require_admin/show_in_sidebar defaults.
        create_data: dict[str, Any] = {
            "type": "lovelace/dashboards/create",
            "url_path": url_path,
            "title": title or url_path.replace("-", " ").title(),
            **{
                key: value
                for key, value in {
                    "icon": icon or None,
                    "require_admin": require_admin,
                    "show_in_sidebar": show_in_sidebar,
                }.items()
                if value is not None
            },
        }
        create_result = await self._client.send_websocket_message(create_data)

        if isinstance(create_result, dict) and not create_result.get("success", True):
            error_msg = create_result.get("error", {})
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", str(error_msg))
            hyphenated = url_path.replace("_", "-")
            suggestions = None
            if "needs to contain a hyphen" in str(error_msg) and hyphenated != url_path:
                suggestions = [f"Try '{hyphenated}' instead"]
            raise_tool_error(
                create_error_response(
                    ws_failure_code(create_result),
                    str(error_msg),
                    suggestions=suggestions,
                    context={"action": "create", "url_path": url_path},
                )
            )

        if isinstance(create_result, dict) and "result" in create_result:
            dashboard_info = create_result["result"]
            return cast(str | None, dashboard_info.get("id"))
        if isinstance(create_result, dict):
            return cast(str | None, create_result.get("id"))
        return None

    async def _send_dashboard_metadata_update(
        self, dashboard_id: str, metadata_update_fields: dict[str, Any], url_path: str
    ) -> None:
        """Send the ``lovelace/dashboards/update`` WS call for metadata-only changes."""
        meta_update: dict[str, Any] = {
            "type": "lovelace/dashboards/update",
            "dashboard_id": dashboard_id,
            **metadata_update_fields,
        }
        meta_result = await self._client.send_websocket_message(meta_update)
        if isinstance(meta_result, dict) and not meta_result.get("success", True):
            error_msg = meta_result.get("error", {})
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", str(error_msg))
            raise_tool_error(
                create_error_response(
                    code=ErrorCode.SERVICE_CALL_FAILED,
                    message=f"Failed to update dashboard metadata: {error_msg}",
                    context={"action": "update", "url_path": url_path},
                )
            )

    async def _update_dashboard_metadata(
        self,
        url_path: str,
        existing_dashboards: list[dict[str, Any]],
        *,
        title: str | None,
        icon: str | None,
        require_admin: bool | None,
        show_in_sidebar: bool | None,
    ) -> tuple[str | None, bool, str | None]:
        """Update metadata for an existing dashboard if any metadata params were provided.

        Returns ``(dashboard_id, metadata_updated, warning)``.
        """
        dashboard_id = None
        for dashboard in existing_dashboards:
            if dashboard.get("url_path") == url_path:
                dashboard_id = dashboard.get("id")
                break

        metadata_update_fields: dict[str, Any] = {
            k: v
            for k, v in {
                "title": title,
                "require_admin": require_admin,
                "show_in_sidebar": show_in_sidebar,
            }.items()
            if v is not None
        }
        if icon is not None:
            # Core's update schema accepts None and then drops the stored icon.
            metadata_update_fields["icon"] = icon or None
        if metadata_update_fields and dashboard_id is not None:
            await self._send_dashboard_metadata_update(
                dashboard_id, metadata_update_fields, url_path
            )
            return dashboard_id, True, None
        if metadata_update_fields and dashboard_id is None:
            # Dashboard ID not found in storage list (e.g. default lovelace on
            # fresh installs). Metadata update via lovelace/dashboards/update
            # is not possible without a storage ID. The caller either proceeds
            # with a requested config write or rejects the resulting no-op.
            warning = (
                "Metadata fields were provided but could not be applied: "
                "dashboard has no storage ID (likely the built-in default dashboard)."
            )
            return dashboard_id, False, warning
        return dashboard_id, False, None

    async def _ensure_dashboard_exists(
        self,
        url_path: str,
        *,
        title: str | None,
        icon: str | None,
        require_admin: bool | None,
        show_in_sidebar: bool | None,
        pre_fetched_dashboards: list[dict[str, Any]] | None,
    ) -> tuple[bool, str | None, bool, str | None]:
        """Create the dashboard if missing, else update its metadata if requested.

        Returns ``(dashboard_exists, dashboard_id, metadata_updated, warning)`` —
        ``dashboard_exists`` reflects state *before* this call, so the caller
        can distinguish create vs update for the response's
        ``action``/``dashboard_created`` fields.
        """
        dashboard_exists, existing_dashboards = await self._lookup_existing_dashboards(
            url_path, pre_fetched_dashboards
        )
        if not dashboard_exists:
            dashboard_id = await self._create_dashboard(
                url_path,
                title=title,
                icon=icon,
                require_admin=require_admin,
                show_in_sidebar=show_in_sidebar,
            )
            return dashboard_exists, dashboard_id, False, None

        dashboard_id, metadata_updated, warning = await self._update_dashboard_metadata(
            url_path,
            existing_dashboards,
            title=title,
            icon=icon,
            require_admin=require_admin,
            show_in_sidebar=show_in_sidebar,
        )
        return dashboard_exists, dashboard_id, metadata_updated, warning

    async def _check_dashboard_replace_hash(
        self,
        url_path: str,
        config_hash: str | None,
        replacement_config: dict[str, Any],
    ) -> str | None:
        """Validate replacement safety/hash and warn on large full-config replacement.

        Omitting ``config_hash`` remains the force-replace path, but the current
        config must still be readable so strategy-dashboard protection can be
        evaluated before any replacement write. The one exception is an existing
        storage dashboard with no config yet and no supplied ``config_hash``:
        there is no strategy state to protect, so its first full config may be
        written. A supplied hash still conflicts with the now-absent config.
        """
        try:
            existing_config, existing_hash = await _get_dashboard_config_internal(
                self._client, url_path
            )
        except ToolError as exc:
            if _is_no_stored_dashboard_config_error(exc):
                if config_hash is None:
                    return None
                self._raise_dashboard_hash_conflict(
                    url_path, message="Dashboard has no saved config"
                )
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Cannot verify the current dashboard config before full replacement",
                    details=extract_tool_error_message(exc),
                    suggestions=[
                        "Retry the operation",
                        "Read the dashboard with ha_config_get_dashboard first",
                    ],
                    context={
                        "action": "set",
                        "url_path": url_path,
                        "reason": "load_failed",
                        "write_committed": False,
                        "post_write_verified": False,
                    },
                )
            )

        existing_config_size = len(json.dumps(existing_config))
        if config_hash is not None and existing_hash != config_hash:
            self._raise_dashboard_hash_conflict(url_path)

        self._validate_strategy_dashboard_replacement(
            url_path,
            was_strategy_dashboard="strategy" in existing_config,
            replacement_config=replacement_config,
            action="set",
        )

        return _large_dashboard_replacement_warning(existing_config_size)

    @staticmethod
    def _raise_dashboard_hash_conflict(
        url_path: str, *, message: str = "Dashboard modified since last read (conflict)"
    ) -> NoReturn:
        """Raise the shared optimistic-lock conflict for a full replacement."""
        raise_dashboard_edit_error(
            url_path,
            "conflict",
            message,
            False,
            "set",
            hash_supplied=True,
        )

    _validate_strategy_dashboard_replacement = staticmethod(
        legacy_errors_sync(validate_strategy_dashboard_replacement)
    )

    async def _save_dashboard_config(
        self,
        url_path: str,
        config_dict: dict[str, Any],
        *,
        action: str = "set",
    ) -> None:
        """Save through the legacy API with a consistent outcome for every edit mode."""
        config_save_data: dict[str, Any] = {
            "type": "lovelace/config/save",
            "config": config_dict,
        }
        if url_path and url_path != "lovelace":  # as _get_dashboard_config_internal
            config_save_data["url_path"] = url_path
        try:
            save_result = await self._client.send_websocket_message(config_save_data)
        except ToolError:
            raise
        except asyncio.CancelledError:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Dashboard write outcome unknown: save request was cancelled",
                    suggestions=[
                        "Read the dashboard before retrying to check whether the save applied"
                    ],
                    context={
                        "action": action,
                        "url_path": url_path,
                        "reason": "write_outcome_unknown",
                        "write_committed": None,
                        "post_write_verified": False,
                    },
                )
            )
        except HomeAssistantCommandNotSent as exc:
            raise_dashboard_edit_error(
                url_path, "write_not_sent", str(exc), False, action
            )
        except Exception as exc:  # noqa: BLE001
            exception_to_structured_error(
                exc,
                context={
                    "action": action,
                    "url_path": url_path,
                    "write_committed": None,
                    "reason": "write_outcome_unknown",
                    "post_write_verified": False,
                },
                suggestions=[
                    "Read the dashboard before retrying to check whether the save applied",
                ],
            )

        if isinstance(save_result, dict) and not save_result.get("success", True):
            raise_known_dashboard_save_rejection(save_result, url_path, action)
            error_msg = save_result.get("error", {})
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", str(error_msg))
            # Core may update the live config before persistence raises. An
            # unrecognized error response cannot establish an unwritten edit.
            raise_dashboard_edit_error(
                url_path,
                "write_outcome_unknown",
                f"Failed to save {'transformed' if action == 'python_transform' else 'dashboard'} config: {error_msg}",
                None,
                action,
            )

    async def _apply_dashboard_config(
        self,
        url_path: str,
        config: dict[str, Any] | str,
        config_hash: str | None,
        dashboard_exists: bool,
        *,
        metadata_updated: bool = False,
    ) -> tuple[bool, str | None, dict[str, Any], dict[str, Any] | None]:
        """Preserve prior registry writes on any configuration-update failure."""
        try:
            return await self._prepare_and_save_dashboard_config(
                url_path, config, config_hash, dashboard_exists
            )
        except (asyncio.CancelledError, Exception) as exc:
            if dashboard_exists and not metadata_updated:
                raise
            # Native and legacy outcomes describe only the config command. A
            # preceding create/metadata call already succeeded in either case.
            if isinstance(exc, asyncio.CancelledError):
                error = create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Configuration update cancelled before saving",
                    context={"action": "set", "url_path": url_path},
                    suggestions=["Read the dashboard before deciding whether to retry"],
                )
            elif isinstance(exc, ToolError):
                error = json.loads(str(exc))
            else:
                error = exception_to_structured_error(
                    exc,
                    context={"action": "set", "url_path": url_path},
                    raise_error=False,
                )
            # Validation/read failures precede saving. Save helpers attach an
            # explicit None when they cannot determine the configuration outcome.
            error["config_write_committed"] = error.get("write_committed", False)
            error["write_committed"] = True
            error["dashboard_created"] = not dashboard_exists
            error["metadata_updated"] = metadata_updated
            prior_change = (
                "Dashboard metadata was updated"
                if dashboard_exists
                else "Dashboard was created"
            )
            error["error"]["message"] = (
                f"{prior_change}; configuration update: {error['error']['message']}"
            )
            raise_tool_error(error)
        # CodeQL does not infer the shared helper's NoReturn contract.
        raise AssertionError("unreachable: raise_tool_error always raises")

    async def _prepare_and_save_dashboard_config(
        self,
        url_path: str,
        config: dict[str, Any] | str,
        config_hash: str | None,
        dashboard_exists: bool,
    ) -> tuple[bool, str | None, dict[str, Any], dict[str, Any] | None]:
        """Return (updated, warning, saved config, native result) for either backend."""
        parsed_config = parse_json_param(config, "config")
        if parsed_config is None or not isinstance(parsed_config, dict):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Config parameter must be a dict/object",
                    context={
                        "action": "set",
                        "provided_type": type(parsed_config).__name__,
                    },
                )
            )
        config_dict = cast(dict[str, Any], parsed_config)
        # Creation has no previous config to compare. Match the legacy create
        # path, which ignores a supplied hash; existing entries retain the guard.
        native_result = await edit_dashboard_via_component(
            self._client,
            url_path,
            expected_hash=config_hash if dashboard_exists else None,
            config=config_dict,
        )
        if native_result is not None:
            native_warning = _large_dashboard_replacement_warning(
                native_result["previous_config_size"]
            )
            return True, native_warning, native_result["config"], native_result

        warning: str | None = None
        if dashboard_exists:
            warning = await self._check_dashboard_replace_hash(
                url_path, config_hash, config_dict
            )

        await self._save_dashboard_config(url_path, config_dict)
        return True, warning, config_dict, None

    async def _attach_dashboard_write_result(
        self,
        result: dict[str, Any],
        url_path: str,
        render_config: dict[str, Any] | None,
        native_result: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Reuse native verification without another dashboard fetch."""
        if native_result is None:
            return await _attach_dashboard_render_paths_after_write(
                self._client, result, url_path, render_config
            )
        for key in ("config_hash", "write_committed", "post_write_verified"):
            result[key] = native_result[key]
        if native_result.get("warnings"):
            result.setdefault("warnings", []).extend(native_result["warnings"])
        if native_result["post_write_verified"]:
            _attach_dashboard_render_paths(result, url_path, native_result["config"])
        return cast(dict[str, Any], native_result["config"])

    async def _run_dashboard_config_update(
        self,
        url_path: str,
        config: dict[str, Any] | str | None,
        config_hash: str | None,
        *,
        title: str | None,
        icon: str | None,
        require_admin: bool | None,
        show_in_sidebar: bool | None,
        pre_resolved_from: str | None,
        pre_fetched_dashboards: list[dict[str, Any]] | None,
        return_screenshot: bool,
        screenshot_options: _DashboardScreenshotOptions,
        MandatoryBPS: bool,
    ) -> "dict[str, Any] | ToolResult":
        """Execute config-replacement mode (create-or-update) and return the tool response."""
        reject_malformed_dashboard_config(config, url_path)
        (
            dashboard_exists,
            dashboard_id,
            metadata_updated,
            metadata_warning,
        ) = await self._ensure_dashboard_exists(
            url_path,
            title=title,
            icon=icon,
            require_admin=require_admin,
            show_in_sidebar=show_in_sidebar,
            pre_fetched_dashboards=pre_fetched_dashboards,
        )

        config_updated = False
        native_result: dict[str, Any] | None = None
        render_config: dict[str, Any] | None = None
        warnings: list[str] = []
        if config is not None:
            (
                config_updated,
                config_warning,
                render_config,
                native_result,
            ) = await self._apply_dashboard_config(
                url_path,
                config,
                config_hash,
                dashboard_exists,
                metadata_updated=metadata_updated,
            )
            if config_warning:
                warnings.append(config_warning)

        if dashboard_exists and not config_updated and not metadata_updated:
            if metadata_warning is not None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        "No dashboard changes were applied",
                        details=metadata_warning,
                        suggestions=[
                            "Use a storage-mode dashboard when changing metadata",
                            "Provide config to replace the dashboard configuration",
                        ],
                        context={"action": "set", "url_path": url_path},
                    )
                )
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "No dashboard changes were requested",
                    suggestions=[
                        "Provide config, patch or python_transform to change dashboard content",
                        "Provide a metadata field such as title or icon",
                        "Use ha_config_get_dashboard to read a dashboard without changing it",
                    ],
                    context={"action": "set", "url_path": url_path},
                )
            )

        if metadata_warning:
            if config_updated:
                metadata_warning = f"{metadata_warning} Dashboard config was saved."
            warnings.insert(0, metadata_warning)

        result_dict = set_dashboard_result(
            url_path,
            dashboard_exists=dashboard_exists,
            dashboard_id=dashboard_id,
            config_updated=config_updated,
            metadata_updated=metadata_updated,
            warnings=warnings,
            pre_resolved_from=pre_resolved_from,
        )
        render_config = await self._attach_dashboard_write_result(
            result_dict, url_path, render_config, native_result
        )
        await _attach_dashboard_skill(result_dict, MandatoryBPS, self._client)
        return await _maybe_attach_screenshot(
            result_dict,
            url_path,
            return_screenshot,
            client=self._client,
            config=render_config,
            options=screenshot_options,
        )

    async def delete(self, url_path: str) -> Any:
        try:
            # ``url_path`` is required. Reject empty/whitespace up-front so
            # the caller gets a structured parameter error instead of a
            # misleading "no dashboard found" from the resolver below.
            # Extension of the #1312 validate_identifier_not_empty pattern
            # to the dashboards family per #1313.
            validate_identifier_not_empty(
                url_path,
                "url_path",
                suggestions=[
                    "Pass a dashboard URL path or internal ID (e.g. 'my-dashboard')",
                    "Use ha_config_get_dashboard(list_only=True) to list dashboards",
                ],
                context={"action": "delete"},
            )
            if url_path == "default":
                url_path = "lovelace"
            resolved, dashboards = await _resolve_dashboard(self._client, url_path)
            if dashboards is None:
                _raise_dashboard_registry_read_error(action="delete", url_path=url_path)
            if resolved is None:
                _raise_unresolved_dashboard_delete(url_path, dashboards)
            resolved_id = resolved["id"]

            response = await self._client.send_websocket_message(
                {"type": "lovelace/dashboards/delete", "dashboard_id": resolved_id}
            )

            if isinstance(response, dict) and not response.get("success", True):
                # Core's collection answers not_found when the item is already gone.
                if response.get("error_code") == "not_found":
                    return {
                        "success": True,
                        "action": "delete",
                        "url_path": url_path,
                        "message": "Dashboard already deleted or does not exist",
                    }
                raise_tool_error(
                    create_error_response(
                        ws_failure_code(response),
                        f"Failed to delete dashboard: {response.get('error')}",
                        suggestions=[_DASHBOARD_LIST_SUGGESTION],
                        context={"action": "delete", "url_path": url_path},
                    )
                )

            result: dict[str, Any] = {
                "success": True,
                "action": "delete",
                "url_path": url_path,
                "message": "Dashboard deleted successfully",
            }
            if resolved_id != url_path:
                result["resolved_id"] = resolved_id
            if url_path == "lovelace":
                result["note"] = (
                    "Home Assistant re-registers its auto-generated default "
                    "dashboard at /lovelace."
                )
            return result
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(
                e,
                context={"action": "delete", "url_path": url_path},
                suggestions=[_DASHBOARD_LIST_SUGGESTION],
            )
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable


# =========================================================================
# Dashboard Resource Management Tools
# =========================================================================
# Resource tools have been moved to tools_resources.py for better organization.
# Available tools:
# - ha_config_list_dashboard_resources: List all resources
# - ha_config_set_dashboard_resource: Create/update resources (inline code or URL)
# - ha_config_delete_dashboard_resource: Delete resources
# =========================================================================


class DashboardConfigTools(DashboardContractTools, LegacyDashboards):
    def __init__(self, client: Any) -> None:
        LegacyDashboards.__init__(self, client)
        DashboardContractTools.__init__(self, self)

    ha_config_get_dashboard = tool(
        name="ha_config_get_dashboard",
        tags={"Dashboards"},
        annotations=read_only_hints("Get Dashboard", open_world=True),
    )(log_tool_usage(legacy_errors(DashboardContractTools.ha_config_get_dashboard)))

    ha_config_set_dashboard = tool(
        name="ha_config_set_dashboard",
        tags={"Dashboards"},
        annotations=write_hints(
            "Create or Update Dashboard",
            destructive=True,
            idempotent=False,
            open_world=True,
        ),
    )(
        with_auto_backup(domain="dashboard", id_param="url_path")(
            log_tool_usage(
                legacy_errors(DashboardContractTools.ha_config_set_dashboard)
            )
        )
    )

    ha_config_delete_dashboard = tool(
        name="ha_config_delete_dashboard",
        tags={"Dashboards"},
        annotations=write_hints(
            "Delete Dashboard", destructive=True, idempotent=True, open_world=False
        ),
    )(
        with_auto_backup(domain="dashboard", id_param="url_path")(
            log_tool_usage(
                legacy_errors(DashboardContractTools.ha_config_delete_dashboard)
            )
        )
    )


def register_config_dashboard_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant dashboard configuration tools."""
    register_tool_methods(mcp, DashboardConfigTools(client))


__all__ = [
    "DashboardConfigTools",
    "_MAX_CARD_DEPTH",
    "_SEARCH_MATCH_CAP",
    "_SearchGaps",
    "_find_cards_matching",
    "fetch_dashboards_list",
    "register_config_dashboard_tools",
]
