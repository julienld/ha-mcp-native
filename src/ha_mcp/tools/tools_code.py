"""
Sandboxed custom tool for Home Assistant MCP Server.

Provides an "escape hatch" (ha_manage_custom_tool) that lets LLMs write and run
custom Python code when no existing tool covers the request, with optional
save/reuse and listing of saved tools.  Code runs in pydantic-monty — a
Rust-based sandboxed Python interpreter with no filesystem or arbitrary
network access. Sandbox code can talk to Home Assistant through five external
functions: ``api_get`` and ``api_post`` for the REST API,
``ws_send`` for WebSocket commands, ``call_tool`` for delegating to other
registered MCP tools, and ``delete_saved_tool`` for removing a previously
saved custom tool. ``api_get``/``api_post`` reject absolute URLs so the HA
bearer token cannot be redirected off-instance.

Saved tools persist to disk when ``CODE_MODE_SAVED_TOOLS_PATH`` is set (the
addon sets this by default), letting users build their own "MCP within an
MCP" — a personal library of one-off tools that survives restarts.

**Requires** ``ENABLE_CODE_MODE=true`` (disabled by default).

See: https://github.com/homeassistant-ai/ha-mcp/issues/726
"""

import json
import logging
import urllib.parse
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.server.context import Context
from ha_mcp._vendor.fastmcp.server.dependencies import get_context

from ..backends.legacy import legacy_errors, legacy_errors_sync
from ..client.rest_client import (
    NON_ADMIN_TOKEN_WARNING,
    HomeAssistantAdminRequiredError,
)
from ..config import get_global_settings
from ..errors import ErrorCode
from ..runtime.code_persistence import (
    MAX_SAVED_TOOLS,
    SAVED_TOOLS_SCHEMA_VERSION,
    SavedToolsPersistence,
)
from ..runtime.code_registry import SavedToolsRegistry
from ..runtime.code_runner import run_sandboxed_code
from ..runtime.code_session import CodeSession
from ..runtime.code_validation import SAVE_NAME_PATTERN
from ..runtime.code_validation import _classify_sandbox_error as _classify_sandbox_error
from ..runtime.code_validation import _sandbox_error as _sandbox_error
from ..runtime.code_validation import (
    _validate_custom_tool_code_inputs as _shared_validate_inputs,
)
from ..runtime.code_validation import (
    _validate_custom_tool_modes as _shared_validate_modes,
)
from ..tool_api import ToolFailure
from .backup_access import snapshot_route_refusal
from .helpers import log_tool_usage
from .tool_hints import write_hints
from .util_helpers import BLOCKED_WS_WRITE_COMMANDS

logger = logging.getLogger(__name__)
_MAX_SAVED_TOOLS = MAX_SAVED_TOOLS
_SAVED_TOOLS_SCHEMA_VERSION = SAVED_TOOLS_SCHEMA_VERSION
_SAVE_NAME_PATTERN = SAVE_NAME_PATTERN
_validate_custom_tool_modes = legacy_errors_sync(_shared_validate_modes)
_validate_custom_tool_code_inputs = legacy_errors_sync(_shared_validate_inputs)

# In-memory cache for saved custom tools, optionally persisted to disk.
#
# Lives at module level deliberately: ``ha_manage_custom_tool`` is a
# stateless MCP tool that needs ``run_saved`` / ``list_saved`` to see
# entries written by earlier ``save_as`` calls in the same process.
# Hydrated from ``settings.code_mode_saved_tools_path`` on
# ``register_code_tools`` startup (one-time) and persisted on every
# subsequent ``save_as`` / ``delete_saved_tool``. Per-call request
# scope wouldn't work because ``run_saved`` would never see prior
# saves; ``code_mode_saved_tools_path`` is the documented persistence
# boundary.
#
# WARNING: This is shared across all clients in the same server process.
# One client's saved tools are listable and runnable by every other client,
# and ``save_as`` overwrites by name without an ownership check. OAuth is the
# only mode with distinct per-user credentials for that to cross; standard,
# OIDC and add-on modes are single-tenant already (SECURITY.md § "Standard
# mode is single-tenant"), so every caller there shares one HA token anyway.
#
# That exposure is accepted design, not a pending TODO — SECURITY.md § Scope
# excludes "Saved custom tools (code mode, off by default) visible to other
# clients of the same server process", on the grounds that they are shared
# scaffolding rather than per-user data, that ``run_saved`` executes with the
# caller's own HA token (granting no access the caller lacks), and that any
# client which can reach ha-mcp is a trusted principal (SECURITY.md § "MCP
# clients are trusted principals"). Scoping per session/user would be a
# feature, not a security fix.
_saved_tools: dict[str, dict[str, str]] = {}

# Tools that sandbox code must not call. Includes ``ha_manage_custom_tool``
# itself (prevents recursive self-invocation) plus the four synthetics that
# the categorized-search transform exposes when ``ENABLE_TOOL_SEARCH=true``
# (``ha_search_tools``, ``ha_call_{read,write,delete}_tool``). Without those
# four entries, sandbox code could "launder" a recursive call as
# ``call_tool("ha_call_write_tool", {"name": "ha_manage_custom_tool", ...})``
# — the proxy would then dispatch the underlying tool and the in-sandbox
# guard never fires. The architectural fix lives in
# ``CategorizedSearchTransform`` (excludes pinned tools from category sets
# when code mode is on); this set is the defense-in-depth that closes the
# inner-call path even if the proxy is reachable some other way.
_BLOCKED_TOOLS = frozenset(
    {
        "ha_manage_custom_tool",
        "ha_search_tools",
        "ha_call_read_tool",
        "ha_call_write_tool",
        "ha_call_delete_tool",
    }
)

# Validation for save_as names


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


def _check_api_post_blocked(normalized: str) -> str | None:
    """Return a rejection message if ``normalized`` matches the api_post
    blocklist or a backup control, or ``None`` if the call should proceed.

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
    return snapshot_route_refusal(rest_path=normalized)


# Cap on the number of saved tools to prevent runaway growth. A buggy
# LLM loop could otherwise fill the on-disk file with unique save_as
# names. Enforced both at load (truncate-with-warning) and at save
# (reject the call before mutating the in-memory cache).


# Schema version for the on-disk saved-tools file. Bumped when the shape
# changes so _load_saved_tools can refuse old/new files cleanly. The
# load path checks data["version"] explicitly and refuses anything that
# isn't this number rather than silently re-interpreting it as v1.


# Module-level flag set when _load_saved_tools fails for a reason other
# than "the file doesn't exist yet" (e.g. PermissionError reading an
# existing file). Persistence is suppressed while this is set so we
# don't atomically replace a temporarily-unreadable file with empty
# content and destroy whatever was on disk. Cleared by a successful
# load at register_code_tools time. The variable is module-level
# (not closure-captured) so save sites in ha_manage_custom_tool /
# _delete_saved_tool can read it without parameter plumbing.
_saved_tools_load_failed = False


def _load_saved_tools(path_str: str) -> dict[str, dict[str, str]]:
    global _saved_tools_load_failed
    persistence = SavedToolsPersistence()
    loaded = persistence.load(path_str)
    _saved_tools_load_failed = persistence.load_failed
    return loaded


def _save_saved_tools(path_str: str, tools: dict[str, dict[str, str]]) -> bool:
    return SavedToolsPersistence(load_failed=_saved_tools_load_failed).save(
        path_str, tools
    )


def _collect_content_texts(content: Any) -> list[str]:
    """Extract text strings from a FastMCP content-block list."""
    texts = []
    for item in content:
        if hasattr(item, "text"):
            texts.append(item.text)
        elif isinstance(item, str):
            texts.append(item)
    return texts


def _decode_tool_result_texts(texts: list[str], is_error: bool) -> Any:
    """Combine content texts, JSON-decode if possible, and wrap errors.

    The error shape matches the ``api_get``/``api_post``/``ws_send``
    failure path so sandbox code can do ``result.get("error")`` uniformly.
    """
    combined = "\n".join(texts)
    try:
        payload: Any = json.loads(combined)
    except (json.JSONDecodeError, TypeError):
        payload = combined
    if is_error:
        message = payload if isinstance(payload, str) else json.dumps(payload)
        return {"error": message}
    return payload


def _extract_tool_result(result: Any) -> Any:
    """Convert a FastMCP ToolResult to basic Python types for the sandbox.

    FastMCP call_tool may return a ToolResult, a list of content objects,
    or a basic type.  Monty can only handle basic Python types (str, int,
    float, bool, list, dict, None), so we must serialize.

    If the ToolResult flags ``isError``/``is_error``, returns ``{"error": ...}``
    so sandbox code sees the failure instead of treating the raw repr as a
    successful payload. The shape matches the ``api_get``/``api_post``/
    ``ws_send`` failure path so user code can do ``result.get("error")``
    uniformly.
    """
    # Already a basic type — pass through
    if isinstance(result, (str, int, float, bool, type(None), dict)):
        return result

    # ``list`` is also a basic type Monty handles, but a list might also
    # be FastMCP's "list of content objects" shape (each element having
    # a ``.text`` or being a content-block ``dict``). Distinguish: if the
    # first element looks like a content object, treat as a ToolResult
    # payload; otherwise pass through as a normal list of basic values.
    if isinstance(result, list):
        looks_like_content = bool(result) and (
            hasattr(result[0], "text") or hasattr(result[0], "type")
        )
        if not looks_like_content:
            return result

    # ToolResult or similar: extract content list
    content = None
    if hasattr(result, "content"):
        content = result.content
    elif isinstance(result, list):
        content = result

    is_error = bool(getattr(result, "is_error", False))

    if content:
        texts = _collect_content_texts(content)
        if texts:
            return _decode_tool_result_texts(texts, is_error)

    # Fallback: opaque object with no recognized content. Log so the
    # str(result) repr doesn't silently masquerade as a successful return.
    logger.warning(
        "_extract_tool_result fell through to str() for type=%s isError=%s",
        type(result).__name__,
        is_error,
    )
    repr_str = str(result)
    if is_error:
        return {"error": repr_str}
    return repr_str


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


class _SandboxBridge:
    """Bridges sandboxed Monty code to HA REST/WebSocket/MCP-tool calls.

    These were originally closures defined inline inside
    ``_run_sandboxed_code`` (sharing its ``call_count`` via ``nonlocal``).
    They're grouped here as methods purely to keep each function's
    cyclomatic complexity manageable — no behavior change. One instance
    is created per ``_run_sandboxed_code`` invocation, so ``call_count``
    still starts fresh for every execution.

    **Error shape contract.** All bridge methods return a dict with an
    ``"error"`` key on failure; the value may be either a plain string
    (``api_get`` / ``api_post`` / ``ws_send`` / ``delete_saved_tool`` —
    transport-level failures, validation rejections) or a structured
    sub-dict ``{"code": "<ErrorCode>", "message": "<text>"}`` from
    ``_sandbox_error`` (``call_tool`` — propagates the underlying tool's
    ``ErrorCode`` so sandbox code can branch on category). The structured
    form also includes ``"success": False`` at the top level. **Consumers
    should always probe with ``if "error" in result:``** — never
    ``result["error"].lower()`` blindly, because the value isn't always a
    string.
    """

    def __init__(self, ctx: Context, client: Any, settings: Any) -> None:
        self.ctx = ctx
        self.client = client
        self.settings = settings
        self.call_count = 0

    def _over_invocation_limit(self) -> bool:
        """Increment the shared call counter; return True once the per-execution cap is exceeded."""
        self.call_count += 1
        return bool(self.call_count > self.settings.code_mode_max_invocations)

    async def api_get(self, endpoint: str) -> Any:
        """GET request to Home Assistant REST API."""
        if self._over_invocation_limit():
            return {
                "error": f"API call limit exceeded ({self.settings.code_mode_max_invocations})"
            }
        try:
            normalized = _normalize_endpoint(endpoint)
        except ValueError as exc:
            logger.warning("api_get rejected endpoint %r: %s", endpoint, exc)
            return {"error": str(exc)}
        try:
            response = await self.client.guarded_request("GET", normalized)
            try:
                return response.json()
            except json.JSONDecodeError:
                return response.text
        except HomeAssistantAdminRequiredError as exc:
            return {"error": str(exc), "warnings": [NON_ADMIN_TOKEN_WARNING]}
        except Exception as exc:
            logger.warning("api_get(%r) failed", endpoint, exc_info=True)
            return {"error": str(exc)[:200]}

    async def api_post(self, endpoint: str, data: dict[str, Any] | None = None) -> Any:
        """POST request to Home Assistant REST API."""
        if self._over_invocation_limit():
            return {
                "error": f"API call limit exceeded ({self.settings.code_mode_max_invocations})"
            }
        try:
            normalized = _normalize_endpoint(endpoint)
        except ValueError as exc:
            logger.warning("api_post rejected endpoint %r: %s", endpoint, exc)
            return {"error": str(exc)}
        block_reason = _check_api_post_blocked(normalized)
        if block_reason is not None:
            # INFO-level so blocked attempts are visible in operator logs
            # for forensics without flooding INFO during normal operation.
            logger.info(
                "sandbox.api_post.blocked endpoint=%r normalized=%r",
                endpoint,
                normalized,
            )
            return {"error": block_reason}
        # State-changing call: DEBUG-level audit trail. Operators can
        # bump the ha_mcp.tools.tools_code logger to DEBUG to see what
        # the sandbox is actually doing on their HA instance.
        # ``map(str, ...)`` on the keys because Monty allows mixed-type
        # dict keys (e.g. ``{1: "x", "a": "y"}``); a plain ``sorted``
        # would raise TypeError on the first invocation and the user
        # would see a confusing "api_post failed" with no hint that
        # the audit-log step was the real culprit.
        logger.debug(
            "sandbox.api_post endpoint=%r data_keys=%s",
            endpoint,
            sorted(map(str, data.keys())) if isinstance(data, dict) else None,
        )
        try:
            post_kwargs: dict[str, Any] = {}
            if data is not None:
                post_kwargs["json"] = data
            response = await self.client.guarded_request(
                "POST", normalized, **post_kwargs
            )
            try:
                return response.json()
            except json.JSONDecodeError:
                return response.text
        except HomeAssistantAdminRequiredError as exc:
            return {"error": str(exc), "warnings": [NON_ADMIN_TOKEN_WARNING]}
        except Exception as exc:
            logger.warning("api_post(%r) failed", endpoint, exc_info=True)
            return {"error": str(exc)[:200]}

    async def ws_send(self, message: Any) -> Any:
        """Send a Home Assistant WebSocket command and return its result.

        ``message`` must be a dict with at least a ``type`` field, e.g.
        ``{"type": "config/area_registry/list"}``.  The MCP server's shared
        WebSocket client adds the message ``id`` and handles auth, so the
        sandbox should not include either. Typed as ``Any`` because sandbox
        code is dynamic and may pass non-dict values; the runtime guard
        below converts that into an error dict.

        Commands in ``BLOCKED_WS_WRITE_COMMANDS`` are rejected: they rewrite
        state with no sandbox use case (``config/core/update``) or skip their
        wrapping tool's validation (dashboard collision, registry invariants).
        Full-snapshot backup commands follow the backup controls.
        """
        if self._over_invocation_limit():
            return {
                "error": f"WebSocket call limit exceeded ({self.settings.code_mode_max_invocations})"
            }
        if not isinstance(message, dict):
            return {"error": "ws_send(message) requires a dict with a 'type' field"}
        msg_type = message.get("type")
        if not isinstance(msg_type, str):
            return {"error": "ws_send(message) requires a 'type' field"}
        if msg_type in BLOCKED_WS_WRITE_COMMANDS:
            logger.info("sandbox.ws_send.blocked type=%r", msg_type)
            return {
                "error": (
                    f"WebSocket command {msg_type!r} is blocked from the "
                    "sandbox. Use the corresponding wrapping tool via "
                    "call_tool (e.g. ha_config_set_dashboard, "
                    "ha_set_area_or_floor, ha_set_device, ha_set_entity) "
                    "so validation runs."
                )
            }
        if refusal := snapshot_route_refusal(ws_command=msg_type, ws_params=message):
            return {"error": refusal}
        logger.debug("sandbox.ws_send type=%r", msg_type)
        try:
            return await self.client.send_websocket_message(message)
        except Exception as exc:
            logger.warning("ws_send(type=%r) failed", msg_type, exc_info=True)
            return {"error": str(exc)[:200]}

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Bridge: sandbox code → MCP tool execution."""
        # Counter increments first so blocked-tool calls also count toward
        # the per-execution cap; otherwise a tight loop on a blocked name
        # would never trip the limit.
        if self._over_invocation_limit():
            return _sandbox_error(
                ErrorCode.VALIDATION_FAILED,
                f"call_tool limit exceeded ({self.settings.code_mode_max_invocations} "
                f"calls per execution)",
            )

        if tool_name in _BLOCKED_TOOLS:
            return _sandbox_error(
                ErrorCode.AUTH_INSUFFICIENT_PERMISSIONS,
                f"Tool '{tool_name}' cannot be called from sandbox code",
            )

        try:
            result = await self.ctx.fastmcp.call_tool(tool_name, arguments)
        except ToolError as te:
            try:
                return json.loads(str(te))
            except (json.JSONDecodeError, TypeError):
                return _sandbox_error(ErrorCode.INTERNAL_ERROR, str(te))
        except Exception as exc:
            logger.warning("call_tool(%r) failed", tool_name, exc_info=True)
            return _sandbox_error(
                ErrorCode.INTERNAL_ERROR,
                f"Tool call failed: {str(exc)[:200]}",
            )

        # FastMCP call_tool returns a ToolResult or list of content objects.
        # Monty can only handle basic Python types, so serialize everything.
        return _extract_tool_result(result)

    def delete_saved_tool(self, name: Any) -> dict[str, Any]:
        return SavedToolsRegistry(_saved_tools, _save_saved_tools).delete(
            name, self.settings.code_mode_saved_tools_path
        )


async def _run_sandboxed_code(
    code: str, ctx: Context, client: Any, settings: Any, Monty: Any, ResourceLimits: Any
) -> Any:
    bridge = _SandboxBridge(ctx, client, settings)
    return await run_sandboxed_code(
        code,
        {
            "api_get": bridge.api_get,
            "api_post": bridge.api_post,
            "ws_send": bridge.ws_send,
            "call_tool": bridge.call_tool,
            "delete_saved_tool": bridge.delete_saved_tool,
        },
        settings,
        Monty,
        ResourceLimits,
    )


def _build_list_saved_tools_response() -> dict[str, Any]:
    return SavedToolsRegistry(_saved_tools, _save_saved_tools).list()


@legacy_errors_sync
def _apply_custom_tool_save_as(
    response: dict[str, Any], save_as: str, code: str, justification: str, settings: Any
) -> None:
    SavedToolsRegistry(_saved_tools, _save_saved_tools).save_as(
        response, save_as, code, justification, settings.code_mode_saved_tools_path
    )


def register_code_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register the ha_manage_custom_tool sandboxed code execution tool.

    Skips registration entirely when ``ENABLE_CODE_MODE`` is ``False``
    (the default) so the tool never appears in the tool catalog.
    """
    settings = get_global_settings()
    if not settings.enable_code_mode:
        logger.debug("Code mode disabled — skipping ha_manage_custom_tool registration")
        return

    try:
        from pydantic_monty import Monty, ResourceLimits
    except ImportError:
        logger.warning(
            "pydantic-monty is not installed — ha_manage_custom_tool will be "
            "unavailable. Install with: pip install pydantic-monty"
        )
        return

    logger.info(
        "Code mode enabled — registering ha_manage_custom_tool "
        "(max_duration=%.1fs, max_memory=%d bytes)",
        settings.code_mode_max_duration,
        settings.code_mode_max_memory,
    )

    # Hydrate the saved-tools cache from disk if persistence is enabled.
    # _saved_tools is a module-level dict; clear-and-update keeps the
    # same identity so other module-level references (e.g. the run_saved
    # / list_saved branches below) see the loaded data without lookup
    # changes.
    if settings.code_mode_saved_tools_path:
        loaded = _load_saved_tools(settings.code_mode_saved_tools_path)
        _saved_tools.clear()
        _saved_tools.update(loaded)

    async def list_saved() -> dict[str, Any]:
        return _build_list_saved_tools_response()

    async def get_saved(name: str) -> dict[str, str] | None:
        return _saved_tools.get(name)

    async def save_tool(
        response: dict[str, Any], name: str, code: str, justification: str
    ) -> None:
        _apply_custom_tool_save_as(response, name, code, justification, settings)

    async def run_code(code: str) -> Any:
        return await _run_sandboxed_code(
            code, get_context(), client, settings, Monty, ResourceLimits
        )

    session = CodeSession(
        list_saved=list_saved,
        get_saved=get_saved,
        save_tool=save_tool,
        run_code=run_code,
        passthrough_errors=(ToolFailure, ToolError),
    )
    mcp.tool(
        name="ha_manage_custom_tool",
        tags={"System", "beta"},
        annotations=write_hints(
            "Custom Tool", destructive=True, idempotent=False, open_world=False
        ),
    )(log_tool_usage(legacy_errors(session.ha_manage_custom_tool)))
