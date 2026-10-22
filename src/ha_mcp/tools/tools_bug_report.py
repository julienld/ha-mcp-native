"""
Bug report tool for Home Assistant MCP Server.

This module provides a tool to collect diagnostic information and guide users
on how to create effective bug reports.
"""

import asyncio
import importlib
import importlib.metadata
import importlib.util
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Literal

import httpx

from ha_mcp import __version__
from ha_mcp._vendor.fastmcp import Context
from ha_mcp._vendor.fastmcp.server.dependencies import get_http_headers
from ha_mcp._vendor.fastmcp.tools import tool

from .._version import is_embedded, is_running_in_addon
from ..backends.legacy import legacy_errors
from ..client.supervisor_client import make_supervisor_httpx_client
from ..config import Settings, get_global_settings
from ..errors import create_validation_error
from ..runtime.report_environment import _detect_platform as _detect_platform
from ..runtime.report_rendering import (
    _AGENT_SIDE_TEMPLATES as _AGENT_SIDE_TEMPLATES,
)
from ..runtime.report_rendering import (
    _ISSUE_TITLE_MAX_CHARS as _ISSUE_TITLE_MAX_CHARS,
)
from ..runtime.report_rendering import (
    _TITLE_PREFIXES as _TITLE_PREFIXES,
)
from ..runtime.report_rendering import (
    KNOWN_CLIENT_ISSUES_HINT as KNOWN_CLIENT_ISSUES_HINT,
)
from ..runtime.report_rendering import (
    MISSING_TOOL_HINT as MISSING_TOOL_HINT,
)
from ..runtime.report_rendering import (
    _build_formatted_report as _build_formatted_report,
)
from ..runtime.report_rendering import (
    _format_logs_for_report as _format_logs_for_report,
)
from ..runtime.report_rendering import (
    _format_startup_logs as _format_startup_logs,
)
from ..runtime.report_rendering import (
    _format_websockets_dependency_value as _format_websockets_dependency_value,
)
from ..runtime.report_rendering import (
    _generate_anonymization_guide as _generate_anonymization_guide,
)
from ..runtime.report_rendering import (
    _issue_title as _issue_title,
)
from ..runtime.report_rendering import (
    render_report,
)
from ..runtime.report_tools import ReportFacade
from ..utils.mcp_client_host import detect_client_host
from ..utils.usage_logger import (
    AVG_LOG_ENTRIES_PER_TOOL,
    get_recent_logs,
    get_startup_logs,
)
from .bug_report_config import collect_config_toggles
from .bug_report_templates import (
    _ReportText,
    _sanitize_log_text,
)
from .coercion import ANSI_ESCAPE_RE, parse_string_list_param
from .component_api import InfoResult, read_component_version, read_info, webhook_auth
from .helpers import (
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .server_version import detect_installed_version, version_mismatch
from .tool_hints import read_only_hints

logger = logging.getLogger(__name__)

# Report types whose body carries the tool-call log, environment and
# configuration, but no startup, app or Core error logs.


# GitHub refuses a longer issue title.

# Guidance surfaced when the reported problem is a tool that never shows up in
# the client's tool list. In issue #1804 this produced a false bug report: the
# tool was enabled and loaded server-side (startup logs even said so), but the
# MCP client had never refreshed its cached tool list, so the agent concluded
# it was a server bug. A stale client-side tool list is the single most likely
# cause of a missing tool and must be ruled out before filing.

# Known Claude Desktop bugs that present as ha-mcp failures. Both live in
# the client, never reach this server, and already have upstream tickets, so
# the agent must rule them out before filing here.

# Max characters to include from addon container logs.
# 3000 chars ≈ 750 LLM tokens — keeps the tool response well below context budgets
# while still capturing enough recent output to diagnose most issues.
_ADDON_LOG_MAX_CHARS = 3000

# Max characters to include from the Home Assistant error log (home-assistant.log).
# Slightly larger than the addon cap: this log carries the highest-value
# diagnostic lines (auth failures, integration tracebacks) and the recent tail
# is where they land. ~5000 chars ≈ 1250 LLM tokens.
_CORE_LOG_MAX_CHARS = 5000

# Log window requested from the install for that tail. Only the last
# _CORE_LOG_MAX_CHARS survive into the report — a few dozen lines of typical
# HA log text — so anything deeper is fetched and then thrown away, which on
# Supervisor-backed installs is exactly the cost that made the unconditional
# 20,000-line fetch hang (#2279). 500 keeps an order of magnitude of headroom
# for short lines.
_CORE_LOG_WINDOW_LINES = 500

# A slow or missing Supervisor must not hold up the whole report.
_SUPERVISOR_PROBE_TIMEOUT = 10.0


def _detect_installation_method() -> str:
    """
    Detect how ha-mcp was installed.

    Returns one of: embedded, addon, docker, git, pypi, unknown
    """
    # 1. In-process server inside HA core (the ha_mcp_tools custom
    #    component's "server" entry). Checked BEFORE the docker probe: the
    #    HA core container carries /.dockerenv, so without this branch
    #    embedded installs misreport as plain docker.
    if is_embedded():
        return "embedded"

    # 2. Home Assistant Add-on (has supervisor token)
    if is_running_in_addon():
        return "addon"

    # 3. Docker container (non-addon)
    if Path("/.dockerenv").exists():
        return "docker"

    # 4. Git clone - check for .git directory relative to package
    try:
        # Go up from tools_bug_report.py -> tools -> ha_mcp -> src -> project_root
        project_root = Path(__file__).parent.parent.parent.parent
        if (project_root / ".git").exists():
            return "git"
    except Exception:  # noqa: BLE001
        # Best-effort probe: path resolution may fail in unusual layouts;
        # fall through to the next detection heuristic.
        pass

    # 5. PyPI install - marker file exists in package
    try:
        marker_path = Path(__file__).parent.parent / "_pypi_marker"
        if marker_path.exists():
            return "pypi"
    except Exception:  # noqa: BLE001
        # Best-effort probe: marker lookup may fail in unusual layouts;
        # fall through to the default "unknown" result.
        pass

    # 6. Default - unknown
    return "unknown"


def _instance_identity() -> dict[str, Any]:
    """Return process identity (id, start time, uptime).

    Mirrors the ``/api/settings/info`` fields so a report and the web UI can
    be matched to the same (or different) server process.
    """
    from ..settings_ui import _PROCESS_INSTANCE_ID, _PROCESS_STARTED_AT

    return {
        "instance_id": _PROCESS_INSTANCE_ID,
        "started_at": _PROCESS_STARTED_AT,
        "uptime_seconds": round(time.time() - _PROCESS_STARTED_AT, 1),
    }


def _websockets_dependency_state() -> dict[str, Any]:
    """Report the vendored websockets copy plus the shared copy's state.

    ha-mcp runs on its private ``ha_mcp._vendor.websockets``, immune to the
    shared site-packages copy that ~20 HA integration libraries contend
    over (#2135/#2146). The vendored version proves what the server
    actually runs; the shared copy's metadata version is ecosystem context
    for triage (a torn shared copy no longer affects ha-mcp, but still
    breaks the integrations that use it). Any probe failure is captured as
    data — the bug-report path itself must never break on a broken
    dependency.
    """
    state: dict[str, Any] = {}
    try:
        vendored = importlib.import_module("ha_mcp._vendor.websockets")
        importlib.import_module("ha_mcp._vendor.websockets.asyncio.client")
        state["vendored_version"] = getattr(vendored, "__version__", "Unknown")
        state["vendored_import_ok"] = True
    except Exception as e:  # noqa: BLE001
        state["vendored_version"] = None
        state["vendored_import_ok"] = False
        state["vendored_import_error"] = f"{type(e).__name__}: {e}"
    # Whether the vendored copy has its optional C accelerator. The vendor
    # sync ships pure Python only (scripts/vendor_websockets.py strips the
    # compiled extension), so this answers "did vendoring cost throughput?"
    # without the reporter having to guess.
    state["vendored_c_speedups"] = (
        importlib.util.find_spec("ha_mcp._vendor.websockets.speedups") is not None
    )
    # Shared-copy context for triage, read from DIST METADATA only: ha-mcp
    # never imports the shared copy, and probing it by import would mean
    # touching the contested package this whole design avoids. Absent and
    # unreadable are reported distinctly — collapsing a corrupt install into
    # "absent" would read as a clean environment during exactly the failure
    # class this field exists for.
    try:
        state["shared_metadata_version"] = importlib.metadata.version("websockets")
    except importlib.metadata.PackageNotFoundError:
        state["shared_metadata_version"] = None
    except Exception as e:  # noqa: BLE001
        state["shared_metadata_version"] = None
        state["shared_metadata_error"] = f"{type(e).__name__}: {e}"
    return state


def _get_config_toggles(settings: Settings | None = None) -> dict[str, Any]:
    """Read tool-surface-shaping config toggles from Settings.

    Defaults to global settings; tests can pass Settings instead. Settings
    lookup failures return an empty dict; tool-state failures preserve the
    known settings and mark tool configuration diagnostics unavailable.
    """
    try:
        s = settings if settings is not None else get_global_settings()

        return collect_config_toggles(s)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Failed to read settings for bug report toggles: %s (%s)",
            e,
            type(e).__name__,
        )
        return {}


def _tool_policy_summary() -> str:
    """Describe the stored tool security policy in one line, best-effort.

    The import stays inside the ``try``: the server keeps running when the
    policy package fails to import, and the report must still work then.
    """
    try:
        from ..policy.persistence import load_policy
        from ..utils.data_paths import get_data_dir

        policy = load_policy(get_data_dir())
    except Exception as e:  # noqa: BLE001
        logger.warning("Tool policy probe failed: %s (%s)", e, type(e).__name__)
        # load_policy's message can quote rule contents, so only its kind
        # reaches the report.
        if "not valid JSON" in str(e):
            return "unreadable (invalid JSON)"
        if "schema validation" in str(e):
            return "unreadable (schema validation failed)"
        return f"unreadable ({type(e).__name__})"
    count = len(policy.rules)
    rules = "1 rule" if count == 1 else f"{count} rules"
    return f"{rules}, rule_effect={policy.rule_effect}"


def _extract_client_info(ctx: Context | None) -> dict[str, str]:
    """Pull the connecting MCP client's self-identification off the request context.

    The MCP ``initialize`` handshake carries a ``clientInfo`` Implementation
    object (``name``/``version``/optional ``title``). FastMCP exposes the
    underlying server session as ``ctx.session``; the MCP SDK's
    ``ServerSession`` keeps the parsed initialize params on ``client_params``;
    on the 2026-07-28 protocol the SDK synthesizes them from the request's
    ``_meta`` when it carries both client info and capabilities. An
    older-protocol client over stateless HTTP sends client info only in
    ``initialize``, so the request's ``User-Agent`` is the fallback there,
    marked as such in ``title``.

    Returns ``{"name": ..., "version": ..., "title": ...}``. ``name`` and
    ``version`` fall back to ``"unknown"`` when the client didn't send them;
    ``title`` falls back to the empty string so callers can distinguish "not
    sent" from a real title without false-positive aside rendering.

    Returns an empty dict when there is no context, or no client info and no
    ``User-Agent`` (e.g. stdio), so the bug-report path stays robust. The
    log level is intentionally INFO, not DEBUG: this catch is the only signal
    we'd get if FastMCP/MCP SDK shape drifts in a future release, and silent
    drift would hide a regression for months.
    """
    if ctx is None:
        return {}
    try:
        session = getattr(ctx, "session", None)
        params = (
            getattr(session, "client_params", None) if session is not None else None
        )
        client = (
            getattr(params, "client_info", None) or getattr(params, "clientInfo", None)
            if params is not None
            else None
        )
        if client is None:
            return _client_info_from_user_agent()
        return {
            "name": getattr(client, "name", None) or "unknown",
            "version": getattr(client, "version", None) or "unknown",
            "title": getattr(client, "title", None) or "",
        }
    except Exception as e:  # noqa: BLE001
        logger.info(
            "Failed to read MCP client info from context: %s (%s)",
            e,
            type(e).__name__,
        )
        return {}


def _client_info_from_user_agent() -> dict[str, str]:
    """``{"name", "version", "title"}`` from the HTTP ``User-Agent``, or ``{}``."""
    user_agent = get_http_headers(include={"user-agent"}).get("user-agent", "").strip()
    if not user_agent:
        return {}
    product = user_agent.split()[0]
    name, _, version = product.partition("/")
    logger.debug("No MCP client info on the request; using User-Agent %r", product)
    return {
        "name": name or "unknown",
        "version": version or "unknown",
        "title": "from HTTP User-Agent",
    }


def _http_user_agent() -> str:
    """The request's ``User-Agent``, or ``""`` outside an HTTP request."""
    value = get_http_headers(include={"user-agent"}).get("user-agent", "")
    return str(value).strip()


def _detect_mcp_transport() -> str:
    """Best-effort MCP transport detection.

    Returns ``stdio`` / ``http`` / ``sse`` / ``unknown``. We can't observe the
    transport perfectly from a tool call, so we look at the entrypoint name
    and well-known env hints. The result is informational — the bug template
    surfaces it as an auto-detect that the agent or user can override.

    ha-mcp ships no SSE entry point, so ``sse`` only appears when an operator
    drives fastmcp's own deprecated SSE transport via ``FASTMCP_TRANSPORT`` —
    worth surfacing in a report, since that run shape is unsupported.
    """
    # Entry-point script name (e.g. ``ha-mcp-web`` for HTTP;
    # pyproject.toml's [project.scripts] is the source of truth).
    argv0 = (sys.argv[0] if sys.argv else "").lower()
    basename = os.path.basename(argv0)
    if basename.endswith("-web"):
        return "http"

    # Env hints set by HTTP wrappers / supervisors. ``streamable-http`` is the
    # documented FastMCP variant; collapse it to ``http`` since the
    # distinction doesn't change triage decisions.
    transport_env = os.environ.get("FASTMCP_TRANSPORT", "").strip().lower()
    if transport_env in {"http", "stdio", "sse"}:
        return transport_env
    if transport_env == "streamable-http":
        return "http"
    if os.environ.get("MCP_HTTP_PORT") or os.environ.get("FASTMCP_PORT"):
        return "http"

    # The in-process server inside HA core only ever serves HTTP, and HA's
    # stdin is not a TTY, so the isatty fallback below would label every
    # embedded install "stdio" (seen on a live component install, 8.5.0).
    if is_embedded():
        return "http"

    # Home Assistant add-on always runs HTTP via homeassistant-addon/start.py.
    # Placed after the explicit hints (argv0 / env) so an operator override
    # still wins, but before the stdin-isatty fallback because Supervisor-
    # launched containers have no TTY on stdin and would otherwise be
    # mislabeled stdio.
    if is_running_in_addon():
        return "http"

    # If stdin is piped (not a TTY), ha-mcp was launched by an MCP host on
    # stdio. If it IS a TTY, this is a manual / interactive run with no
    # other transport hints — fall through to ``unknown``.
    try:
        if not sys.stdin.isatty():
            return "stdio"
    except (AttributeError, OSError, ValueError):
        # ``sys.stdin`` can be None or detached (pythonw, daemonized
        # contexts, certain test harnesses). Treat as no signal.
        pass

    return "unknown"


async def _fetch_addon_logs() -> str:
    """Fetch ha-mcp addon container logs via the Supervisor REST API.

    Only works when running as a Home Assistant add-on (SUPERVISOR_TOKEN set).
    Uses /addons/self/logs which resolves to the calling addon's own logs via
    the Supervisor's per-addon token binding — no slug interpolation needed.

    Direct httpx against ``http://supervisor`` is the documented add-on access
    pattern: it uses the Supervisor token directly (no extra HA hop) and
    preserves the ``self`` shortcut, which the WebSocket ``supervisor/api``
    proxy used by other tools may not.

    Returns sanitized log text (last _ADDON_LOG_MAX_CHARS chars, with a
    truncation marker prepended when truncation occurs), or empty string on
    failure.
    """
    # Redundant with the caller's `install_method == "addon"` gate, but kept
    # as a defensive guard for any direct callers added later.
    if not is_running_in_addon():
        return ""

    try:
        async with make_supervisor_httpx_client(
            timeout=10.0, verify=get_global_settings().verify_ssl
        ) as http_client:
            resp = await http_client.get("/addons/self/logs")
            if resp.status_code != 200:
                logger.info("Addon log fetch returned HTTP %s", resp.status_code)
                return ""

            # Strip ANSI escape codes first, then sanitize, then truncate.
            # Sanitizing before truncating prevents secrets that straddle the
            # truncation boundary from leaking through.
            cleaned = ANSI_ESCAPE_RE.sub("", resp.text)
            sanitized = _sanitize_log_text(cleaned)
            if len(sanitized) > _ADDON_LOG_MAX_CHARS:
                marker = (
                    f"[...truncated, showing last {_ADDON_LOG_MAX_CHARS} of "
                    f"{len(sanitized)} chars...]\n"
                )
                return marker + sanitized[-_ADDON_LOG_MAX_CHARS:]
            return sanitized
    except httpx.RequestError as e:
        logger.warning(f"Failed to fetch addon logs: {e}")

    return ""


async def _fetch_core_error_log(client: Any) -> str:
    """Fetch the Home Assistant error log (home-assistant.log) via the client.

    Works on every install type — ``HomeAssistantClient.get_error_log`` routes
    add-on installs to the Supervisor, supervised/HAOS to the hassio proxy, and
    container/pip to ``/api/error_log``. Captured over REST, which stays up even
    when the WebSocket path is failing; that is exactly the failure mode in
    issue #1694, where the high-value auth lines
    (``InsecureKeyLengthWarning``, "invalid authentication") only appeared in
    home-assistant.log, not in the add-on log this tool already captured.

    Best-effort: returns sanitized text (last ``_CORE_LOG_MAX_CHARS`` chars of a
    ``_CORE_LOG_WINDOW_LINES`` window, with a truncation marker prepended) or an
    empty string on any failure, so a log-fetch problem never breaks the
    bug-report path.
    """
    try:
        page = await client.get_error_log(lines=_CORE_LOG_WINDOW_LINES)
    except Exception as e:  # noqa: BLE001
        # Broad by design — the bug-report path must stay robust whatever the
        # client raises (auth, role, connection, transport). Logged at INFO so
        # a missing error log is visible without alarming on a routine 403.
        logger.info(
            "Could not fetch HA error log for bug report: %s (%s)",
            e,
            type(e).__name__,
        )
        return ""

    if not page.text:
        return ""

    # Strip ANSI, then sanitize, then truncate — sanitizing before truncating
    # keeps a secret straddling the cut boundary from leaking through.
    sanitized = _sanitize_log_text(ANSI_ESCAPE_RE.sub("", page.text))
    if len(sanitized) > _CORE_LOG_MAX_CHARS:
        marker = (
            f"[...truncated, showing last {_CORE_LOG_MAX_CHARS} of "
            f"{len(sanitized)} chars...]\n"
        )
        return marker + sanitized[-_CORE_LOG_MAX_CHARS:]
    return sanitized


# Entry titles assigned by the component's config flow (config_flow.py /
# const.py in custom_components/ha_mcp_tools) — the server code cannot import
# the separately-shipped component package, so the defaults are mirrored here.
# "HA MCP Tools" is the pre-#1853 tools-entry title an unloaded legacy entry
# may still carry. A user-renamed entry lands in the "unrecognized" bucket
# rather than being misclassified.
_SERVER_ENTRY_TITLE = "HA-MCP Server"
_TOOLS_ENTRY_TITLES = frozenset({"HA-MCP File & YAML Tools", "HA MCP Tools"})


def _classify_component_entries(
    entries: list[Any],
) -> tuple[list[str], list[str]]:
    """Split ha_mcp_tools config entries into server-entry and unrecognized.

    Tools-entry titles are dropped: their functional state comes from the
    services probe (``_detect_tools_entry_status``), which sees whether the
    entry actually serves, not just whether it exists.
    """
    server: list[str] = []
    unrecognized: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "untitled")
        state = str(entry.get("state") or "unknown state")
        if title == _SERVER_ENTRY_TITLE:
            server.append(f"added ({state})")
        elif title not in _TOOLS_ENTRY_TITLES:
            unrecognized.append(f'"{title}" ({state})')
    return server, unrecognized


class LegacyBugReportTools:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def _detect_component_version(self, info: InfoResult | None) -> str | None:
        """Read the ha_mcp_tools custom component's version, best-effort.

        Prefers the report's fresh ``ha_mcp_tools/info`` read (``info``): the
        cached capability snapshot would still hold the version running when the
        client first probed, which outlives a component update on a server
        running outside HA; the snapshot only stands in when the fresh read
        fails. A component without a usable ``info`` (the 0.11.0-1.1.0 band has
        services but no such command) falls back to the ``get_caller_token``
        bootstrap service, whose response also carries the manifest version.
        Returns None when the component is not installed or the call fails —
        the report path must never break on it.
        """
        # None means no usable info.
        version = await read_component_version(self._client, info)
        if version is not None:
            return version or None
        try:
            resp = await self._client.call_service(
                "ha_mcp_tools", "get_caller_token", {}, return_response=True
            )
            payload = (
                resp.get("service_response", resp) if isinstance(resp, dict) else {}
            )
            version = payload.get("version") if isinstance(payload, dict) else None
            return str(version) if version else None
        except Exception as e:  # noqa: BLE001
            logger.info("Component version probe failed: %s", e)
            return None

    async def _detect_tools_entry_status(self) -> str | None:
        """Describe the File & YAML Tools entry state for the report, best-effort.

        The ``ha_mcp_tools`` services register only in that entry's setup, so
        the service registry distinguishes "entry set up" from the #1996
        state — integration installed but the second entry never added —
        which the component version alone cannot show. Returns None when the
        probe fails; the report path must never break on it.
        """
        from .tools_filesystem import _bootstrap_service_state

        try:
            domain_registered, bootstrap_registered = await _bootstrap_service_state(
                self._client
            )
        except Exception as e:  # noqa: BLE001
            logger.info("Tools-entry status probe failed: %s", e)
            return None
        if not domain_registered:
            return (
                "not set up — no ha_mcp_tools services registered; add the "
                '"HA-MCP File & YAML Tools" entry via "Add entry" on the '
                "HA-MCP Custom Component integration (or install the "
                "component first)"
            )
        if not bootstrap_registered:
            return "set up, but the component is pre-0.5.0 (update via HACS)"
        return "set up (ha_mcp_tools services registered)"

    async def _detect_server_entry_status(self, info: InfoResult | None) -> str | None:
        """Describe the in-process "HA-MCP Server" entry state, best-effort.

        Both parts of the component share the ``ha_mcp_tools`` domain; the File
        & YAML part is covered by the services probe. Entries are classified by
        flow-assigned title, so e.g. an add-on install that also added a redundant
        server entry shows next to Installation Method. A recognized server entry
        gets the webhook auth mode when the report's ``info`` read has one.
        Returns None when ``config_entries/get`` fails; the report must not break.
        """
        try:
            response = await self._client.send_websocket_message(
                {"type": "config_entries/get", "domain": "ha_mcp_tools"}
            )
            if not isinstance(response, dict) or not response.get("success"):
                return None
            result = response.get("result")
            if not isinstance(result, list):
                return None
        except Exception as e:  # noqa: BLE001
            logger.info("Server-entry status probe failed: %s", e)
            return None
        server, unrecognized = _classify_component_entries(result)
        if server:
            auth = webhook_auth(info)
            return ", ".join(server) + (f", webhook auth: {auth}" if auth else "")
        if unrecognized:
            # A renamed entry cannot be classified by title — report it
            # rather than claiming the server entry is absent.
            return "not identified — unrecognized ha_mcp_tools entries: " + ", ".join(
                unrecognized
            )
        return "not added"

    async def _detect_supervisor_info(self) -> dict[str, str]:
        """Read the Supervisor version and host OS, best-effort.

        App tools go through the Supervisor, so a report about them needs its
        release (#2270, #2278). Called only on a supervised install; a failure
        keeps its error type so a timeout or a rejected token is visible.
        """
        from .tools_addons import _supervisor_api_call

        try:
            response = await asyncio.wait_for(
                _supervisor_api_call(self._client, "/info"),
                timeout=_SUPERVISOR_PROBE_TIMEOUT,
            )
        except Exception as e:  # noqa: BLE001
            logger.info("Supervisor info probe failed: %s (%s)", e, type(e).__name__)
            return {"error": type(e).__name__}
        info = response.get("result")
        if not isinstance(info, dict):
            return {"error": "unexpected response"}
        return {
            "supervisor_version": str(info.get("supervisor") or "unknown"),
            "host_os": str(info.get("operating_system") or "unknown"),
        }

    async def _add_home_assistant_info(self, diagnostic_info: dict[str, Any]) -> None:
        """Fill in connection status, HA config and entity count; never raises."""
        try:
            config = await self._client.get_config()
            diagnostic_info["connection_status"] = "Connected"
            diagnostic_info["home_assistant_version"] = config.get("version", "Unknown")
            diagnostic_info["location_name"] = config.get("location_name", "Unknown")
            diagnostic_info["time_zone"] = config.get("time_zone", "Unknown")
            # Only a supervised install loads the hassio integration. Probing
            # without it only produces an "Unknown command" error.
            if "hassio" in config.get("components", []):
                diagnostic_info["supervisor"] = await self._detect_supervisor_info()
            else:
                diagnostic_info["supervisor"] = {"none": "true"}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to get Home Assistant config: {e}")
            diagnostic_info["connection_status"] = (
                f"Connection Error: {_sanitize_log_text(str(e))}"
            )

        try:
            states = await self._client.get_states()
            if states:
                diagnostic_info["entity_count"] = len(states)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to get entity count: {e}")

    async def report(
        self,
        report_type: Literal[
            "runtime_bug", "agent_behavior", "feature_request"
        ] = "runtime_bug",
        title: str | None = None,
        description: str | None = None,
        user_prompt: str | None = None,
        user_comment: str | None = None,
        tool_calls: str | None = None,
        ai_model: str | None = None,
        client_app: str | None = None,
        tool_call_count: int = 10,
        fields: str | list[str] | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        parsed_fields: list[str] | None = None
        if fields is not None:
            try:
                parsed_fields = parse_string_list_param(
                    fields, "fields", allow_csv=True
                )
            except ValueError as exc:
                raise_tool_error(create_validation_error(str(exc), parameter="fields"))
        text = _ReportText(
            title=title,
            description=description,
            user_prompt=user_prompt,
            tool_calls=tool_calls,
            user_comment=user_comment,
            ai_model=ai_model,
            client_app=client_app,
        )
        install_method = _detect_installation_method()
        platform_info = _detect_platform()
        config_toggles = _get_config_toggles()
        tool_policy = await asyncio.to_thread(_tool_policy_summary)
        mcp_transport = _detect_mcp_transport()
        client_info = _extract_client_info(ctx)
        client_host = (
            await asyncio.to_thread(detect_client_host)
            if mcp_transport == "stdio"
            else {}
        )
        user_agent = _http_user_agent()
        installed_version = await asyncio.to_thread(detect_installed_version)
        component_info = await read_info(self._client)
        component_version = await self._detect_component_version(component_info)
        tools_entry_status = await self._detect_tools_entry_status()
        server_entry_status = await self._detect_server_entry_status(component_info)
        diagnostic_info: dict[str, Any] = {
            "ha_mcp_version": __version__,
            "installed_version": installed_version,
            "version_mismatch": version_mismatch(installed_version),
            "component_version": component_version,
            "tools_entry_status": tools_entry_status,
            "server_entry_status": server_entry_status,
            "supervisor": {"error": "not probed"},
            "instance": _instance_identity(),
            "installation_method": install_method,
            "platform": platform_info,
            "websockets_dependency": _websockets_dependency_state(),
            "mcp_transport": mcp_transport,
            "mcp_client_info": client_info,
            "mcp_client_host": client_host,
            "http_user_agent": user_agent,
            "config_toggles": config_toggles,
            "ignored_disabled_tools": config_toggles.get("ignored_disabled_tools"),
            "tool_config_warnings": config_toggles.get("tool_config_warnings"),
            "tool_config_status": config_toggles.get("tool_config_status"),
            "tool_policy": tool_policy,
            "connection_status": "Unknown",
            "home_assistant_version": "Unknown",
            "entity_count": 0,
        }
        await self._add_home_assistant_info(diagnostic_info)
        max_log_entries = AVG_LOG_ENTRIES_PER_TOOL * 4 * tool_call_count
        recent_logs = get_recent_logs(max_entries=max_log_entries)
        startup_logs = get_startup_logs()
        addon_logs = ""
        if install_method == "addon":
            addon_logs = await _fetch_addon_logs()
        core_error_log = await _fetch_core_error_log(self._client)
        return render_report(
            diagnostic_info,
            recent_logs,
            startup_logs,
            addon_logs,
            core_error_log,
            report_type,
            text,
            parsed_fields,
        )


class BugReportTools(ReportFacade, LegacyBugReportTools):
    def __init__(self, client: Any) -> None:
        LegacyBugReportTools.__init__(self, client)
        ReportFacade.__init__(self, self)

    ha_report_issue = tool(
        name="ha_report_issue",
        tags={"Utilities"},
        annotations=read_only_hints("Report Issue or Feedback", open_world=False),
    )(log_tool_usage(legacy_errors(ReportFacade.ha_report_issue)))


def register_bug_report_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register bug report tools with the MCP server."""
    register_tool_methods(mcp, BugReportTools(client))
