"""
App (add-on) management tools for Home Assistant MCP Server.

Provides tools to manage apps through Supervisor and to call app web APIs using
the applicable route: an explicit container port, direct sibling ingress in app
mode, or Home Assistant Core's Ingress proxy in non-app modes.

Note: These tools only work with Home Assistant OS or Supervised installations.
"""

import asyncio
import json
import logging
import ssl
import time
from typing import Any, ClassVar, Literal, NoReturn
from urllib.parse import unquote, urlsplit

import httpx

from ha_mcp._vendor import websockets
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool
from ha_mcp._vendor.websockets.asyncio.client import ClientConnection
from ha_mcp.backends.legacy import legacy_errors, legacy_errors_sync
from ha_mcp.runtime import app_arrays as _app_arrays
from ha_mcp.runtime import app_proxy_results as _app_proxy
from ha_mcp.runtime import app_values as _app_values
from ha_mcp.runtime.app_dispatch import AppDispatch
from ha_mcp.runtime.app_http_hints import (
    _direct_port_auth_suggestion,
    _direct_port_rejection_suggestion,
    _front_door_state,
    add_http_error_hints,
)
from ha_mcp.runtime.app_results import (
    app_info_result,
    available_apps_result,
    installed_apps_result,
)
from ha_mcp.runtime.app_store import (
    config_submitted_result,
    diff_store_versions,
    store_refresh_result,
)
from ha_mcp.runtime.app_store import store_versions as _store_versions
from ha_mcp.runtime.app_tools import AppTools

from .._version import is_running_in_addon
from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
    HomeAssistantCommandError,
    HomeAssistantCommandNotSent,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
    _is_ssl_error,
)
from ..client.supervisor_client import make_supervisor_httpx_client
from ..errors import (
    ErrorCode,
    create_error_response,
    create_validation_error,
)
from ..redaction import (
    collect_addon_secret_values,
    redaction_enabled,
    register_known_secret_values,
)
from .addon_connection_hints import addon_connection_failure_suggestions
from .coercion import ANSI_ESCAPE_RE
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .tool_hints import read_only_hints, write_hints

_extract_addon_log_level = _app_values._extract_addon_log_level
_merge_options = _app_values._merge_options
_running_addon_slugs = _app_values._running_addon_slugs
_validate_supervisor_slug = legacy_errors_sync(_app_values._validate_supervisor_slug)

_apply_array_ops = legacy_errors_sync(_app_arrays._apply_array_ops)

store_versions = legacy_errors_sync(_store_versions)

logger = logging.getLogger(__name__)

# Maximum response size to return from app (add-on) API calls (50 KB)
_MAX_RESPONSE_SIZE = 50 * 1024

# Supervisor is local to the app network, so connection acquisition should
# remain short even when an app operation needs a multi-minute response budget.
_SUPERVISOR_ACQUIRE_TIMEOUT = 10.0
_SUPERVISOR_AVAILABILITY_SUGGESTION = (
    "Check Home Assistant connection and Supervisor availability"
)
_SUPERVISOR_RESPONSE_TRUNCATION_SUFFIX = (
    f"\n[Supervisor response truncated to {_MAX_RESPONSE_SIZE // 1024} KiB]"
)

# Hard safety cap on WebSocket messages collected per call. `message_limit`
# can lower this but never raise it.
_MAX_WS_MESSAGES = 1000

_slice_ws_messages = _app_proxy._slice_ws_messages


_is_signal_message = _app_proxy._is_signal_message


_summarize_ws_messages = _app_proxy._summarize_ws_messages


_apply_response_transform = legacy_errors_sync(_app_proxy._apply_response_transform)


# Supervisor's per-job-group rejection, matched case-insensitively, plus the
# bounded window and backoff used to ride it out. See _supervisor_api_call.
# The "for job group" tail is load-bearing: JobGroup.acquire raises
# "Another job is running for job group <name>" for the transient per-app
# collision, while the job-level JobConcurrency.REJECT path raises a bare
# "Another job is running" for long operations (OS update, data-disk wipe)
# that must NOT be retried on this schedule.
_JOB_COLLISION_MARKER = "another job is running for job group"
_JOB_COLLISION_RETRY_WINDOW = 60.0
_JOB_COLLISION_RETRY_INITIAL_DELAY = 1.0
_JOB_COLLISION_RETRY_MAX_DELAY = 5.0
# Matches Supervisor's canonical app slug grammar. A whole-segment "." or
# ".." also matches that grammar, but is not a valid identifier here because
# an HTTP client can normalize it as path traversal.


def _supervisor_rest_failure(
    response: httpx.Response,
    error: object,
    response_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize direct REST failure and retain 4xx/5xx status metadata."""
    error_text = str(error)
    result: dict[str, Any] = {"success": False, "error": error_text}
    if response.is_error:
        result["_status_code"] = response.status_code
        if response_data is not None:
            result["_response_data"] = response_data
        return result

    if not error_text.lower().startswith("command failed:"):
        result["error"] = f"Command failed: {error_text}"
    return result


def _supervisor_invalid_response(
    response: httpx.Response,
    error: object,
    endpoint: str,
    method: str,
) -> dict[str, Any]:
    """Classify a malformed direct response without replaying an accepted write."""
    verb = method.upper()
    if response.is_success and verb not in {"GET", "HEAD"}:
        _raise_supervisor_write_outcome_unknown(
            ErrorCode.SERVICE_CALL_FAILED,
            f"Supervisor API {verb} {endpoint} returned an invalid success response; "
            "the request outcome is unknown.",
            endpoint,
            verb,
        )
    return _supervisor_rest_failure(response, error)


def _bounded_supervisor_text(value: str) -> str:
    """Return model-safe Supervisor text, marking any truncation visibly."""
    if len(value) <= _MAX_RESPONSE_SIZE:
        return value
    prefix_size = _MAX_RESPONSE_SIZE - len(_SUPERVISOR_RESPONSE_TRUNCATION_SUFFIX)
    return value[:prefix_size] + _SUPERVISOR_RESPONSE_TRUNCATION_SUFFIX


def _bounded_supervisor_response_text(response: httpx.Response) -> str:
    """Return a model-safe Supervisor response body with visible truncation."""
    return _bounded_supervisor_text(response.text.strip())


def _normalize_supervisor_rest_response(
    response: httpx.Response,
    endpoint: str,
    method: str,
) -> dict[str, Any]:
    """Normalize a direct Supervisor response and retain write ambiguity."""
    verb = method.upper()
    write_outcome_unknown = verb not in {"GET", "HEAD"} and (
        300 <= response.status_code < 400 or response.status_code >= 500
    )
    if write_outcome_unknown:
        response_body = _bounded_supervisor_response_text(response)
        _raise_supervisor_write_outcome_unknown(
            ErrorCode.SERVICE_CALL_FAILED,
            f"Supervisor API {verb} {endpoint} returned HTTP "
            f"{response.status_code}; the request outcome is unknown.",
            endpoint,
            verb,
            status_code=response.status_code,
            response_body=response_body or None,
        )

    try:
        payload = response.json()
    except ValueError:
        body = _bounded_supervisor_response_text(response)
        error_message = (
            body or f"Supervisor returned invalid JSON (HTTP {response.status_code})"
        )
        return _supervisor_invalid_response(response, error_message, endpoint, method)

    if not isinstance(payload, dict):
        return _supervisor_invalid_response(
            response,
            f"Supervisor returned an invalid response: {payload!r}",
            endpoint,
            method,
        )
    result_marker = payload.get("result")
    valid_result_marker = isinstance(result_marker, str) and result_marker in {
        "ok",
        "error",
    }
    if response.is_success and not valid_result_marker:
        return _supervisor_invalid_response(
            response,
            f"Supervisor returned an invalid success response: {payload!r}",
            endpoint,
            method,
        )
    if response.is_error or result_marker != "ok":
        error = payload.get("message") or payload.get("error")
        fallback = f"Supervisor API call failed (HTTP {response.status_code})"
        return _supervisor_rest_failure(
            response, error or fallback, response_data=payload
        )
    return {"success": True, "result": payload.get("data", {})}


def _supervisor_unknown_outcome_suggestions(endpoint: str, method: str) -> list[str]:
    """Return verification guidance suited to the Supervisor operation."""
    action = endpoint.rstrip("/").rsplit("/", 1)[-1]
    if action in {"restart", "rebuild"}:
        return [
            f"The {action} request may have been accepted; do not replay it "
            "automatically",
            f"Inspect Supervisor jobs and logs for {method} {endpoint} before "
            "deciding whether manual action is needed",
        ]
    return [
        "The request may have been accepted; check the relevant durable state "
        "with ha_get_app before retrying",
        f"Check Supervisor jobs and logs for {method} {endpoint}",
    ]


def _raise_supervisor_write_outcome_unknown(
    code: ErrorCode,
    message: str,
    endpoint: str,
    method: str,
    *,
    status_code: int | None = None,
    response_body: str | None = None,
) -> NoReturn:
    """Report an inconclusive Supervisor write without implying replay is safe."""
    context: dict[str, Any] = {
        "endpoint": endpoint,
        "method": method,
        "outcome": "unknown",
    }
    if status_code is not None:
        context["status_code"] = status_code
    if response_body:
        context["response_body"] = response_body
    raise_tool_error(
        create_error_response(
            code,
            message,
            context=context,
            suggestions=_supervisor_unknown_outcome_suggestions(endpoint, method),
        )
    )


async def _supervisor_api_call_via_core(
    client: HomeAssistantClient,
    endpoint: str,
    method: str,
    wait_timeout: float,
    websocket_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Call Supervisor through Core and preserve ambiguous write outcomes."""
    verb = method.upper()
    try:
        result = await client.send_websocket_message(
            {
                "type": "supervisor/api",
                "_wait_timeout": wait_timeout,
                **websocket_kwargs,
            }
        )
    except HomeAssistantCommandNotSent:
        raise
    except (HomeAssistantCommandTimeout, HomeAssistantConnectionError) as exc:
        if verb not in {"GET", "HEAD"}:
            code = (
                ErrorCode.TIMEOUT_OPERATION
                if isinstance(exc, HomeAssistantCommandTimeout)
                else ErrorCode.CONNECTION_FAILED
            )
            _raise_supervisor_write_outcome_unknown(
                code,
                f"Home Assistant WebSocket returned no answer for Supervisor "
                f"{verb} {endpoint}; the request outcome is unknown: {exc}",
                endpoint,
                verb,
            )
        raise

    if (
        verb not in {"GET", "HEAD"}
        and result.get("success") is False
        and result.get("error_code") == "unknown_error"
        and str(result.get("error", "")).strip().casefold() == "command failed:"
    ):
        _raise_supervisor_write_outcome_unknown(
            ErrorCode.SERVICE_CALL_FAILED,
            f"Home Assistant Core returned a blank Supervisor bridge error for "
            f"{verb} {endpoint}; the request outcome is unknown.",
            endpoint,
            verb,
        )
    return result


async def _supervisor_api_call_once(
    client: HomeAssistantClient,
    endpoint: str,
    method: str,
    data: dict[str, Any] | None,
    wait_timeout: float,
    websocket_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Call Supervisor through the transport allowed for this install mode."""
    if not is_running_in_addon():
        return await _supervisor_api_call_via_core(
            client, endpoint, method, wait_timeout, websocket_kwargs
        )

    request_kwargs: dict[str, Any] = {}
    if data is not None or method.upper() == "POST":
        # Supervisor's install/update/rebuild/uninstall handlers validate an
        # optional schema by parsing request.json(), so bodyless POST actions
        # must still carry an empty JSON object.
        request_kwargs["json"] = data or {}
    acquire_timeout = min(wait_timeout, _SUPERVISOR_ACQUIRE_TIMEOUT)
    transport_timeout = httpx.Timeout(
        wait_timeout,
        connect=acquire_timeout,
        pool=acquire_timeout,
    )
    try:
        async with make_supervisor_httpx_client(
            timeout=transport_timeout,
            verify=client.verify_ssl,
        ) as supervisor_client:
            response = await supervisor_client.request(
                method,
                endpoint,
                **request_kwargs,
            )
    except (httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        raise HomeAssistantConnectionError(
            f"Supervisor API {method.upper()} {endpoint} could not start before "
            f"the {acquire_timeout}s connection-acquisition timeout: {exc}"
        ) from exc
    except httpx.TimeoutException as exc:
        verb = method.upper()
        if verb in {"GET", "HEAD"}:
            raise HomeAssistantConnectionError(
                f"Supervisor API {verb} {endpoint} request timeout after "
                f"{wait_timeout}s"
            ) from exc
        _raise_supervisor_write_outcome_unknown(
            ErrorCode.TIMEOUT_OPERATION,
            f"Supervisor API {verb} {endpoint} timed out after {wait_timeout}s; "
            "the request outcome is unknown.",
            endpoint,
            verb,
        )
    except (httpx.RequestError, OSError) as exc:
        verb = method.upper()
        if verb not in {"GET", "HEAD"} and not isinstance(exc, httpx.ConnectError):
            _raise_supervisor_write_outcome_unknown(
                ErrorCode.CONNECTION_FAILED,
                f"Supervisor API {verb} {endpoint} transport failed; "
                f"the request outcome is unknown: {exc}",
                endpoint,
                verb,
            )
        raise HomeAssistantConnectionError(
            f"Failed to connect to Supervisor API {endpoint}: {exc}"
        ) from exc

    return _normalize_supervisor_rest_response(response, endpoint, method)


def _raise_supervisor_api_failure(
    result: dict[str, Any],
    endpoint: str,
) -> NoReturn:
    """Raise the structured exception represented by a non-retryable result."""
    # Both transports land here, and both carry Supervisor's own text: the
    # direct REST payload and the message Core relays over the WebSocket
    # bridge. Bind the size once, where every failure passes.
    error_text = _bounded_supervisor_text(
        str(result.get("error", f"Supervisor API call failed: {endpoint}"))
    )
    status_code = result.get("_status_code")
    response_data = result.get("_response_data")
    if status_code == 401:
        raise_tool_error(
            create_error_response(
                ErrorCode.AUTH_INVALID_TOKEN,
                f"{error_text}. Supervisor rejected the app's managed token.",
                context={"endpoint": endpoint, "status_code": 401},
                suggestions=[
                    "Restart the ha-mcp app to obtain a fresh Supervisor-managed token",
                    "Check Supervisor logs for token validation failures",
                ],
            )
        )
    if status_code == 404:
        raise HomeAssistantAPIError(
            error_text,
            status_code=404,
            response_data=response_data if isinstance(response_data, dict) else None,
        )
    if status_code == 403:
        raise_tool_error(
            create_error_response(
                ErrorCode.AUTH_INSUFFICIENT_PERMISSIONS,
                (
                    f"{error_text}. Supervisor denied the app request; HTTP 403 "
                    "can mean either token rejection or insufficient API role."
                ),
                context={"endpoint": endpoint, "status_code": 403},
                suggestions=[
                    "Restart the app to refresh its Supervisor-managed token",
                    "Check the ha-mcp app's hassio_api and hassio_role configuration",
                    "Check Supervisor logs for an invalid token or missing API permission",
                ],
            )
        )
    if isinstance(status_code, int) and not error_text.lower().startswith(
        "command failed:"
    ):
        error_text = f"Command failed: {error_text}"
    raise HomeAssistantCommandError(error_text)


def _supervisor_result_mapping(
    result: dict[str, Any],
    endpoint: str,
    method: str,
) -> dict[str, Any]:
    """Require the mapping payload used by every supported Supervisor endpoint."""
    payload = result.get("result", {})
    if isinstance(payload, dict):
        return payload

    verb = method.upper()
    message = _bounded_supervisor_text(
        f"Supervisor API {verb} {endpoint} returned an invalid result payload: "
        f"{payload!r}"
    )
    if verb not in {"GET", "HEAD"}:
        _raise_supervisor_write_outcome_unknown(
            ErrorCode.SERVICE_CALL_FAILED,
            f"{message}; the request outcome is unknown.",
            endpoint,
            verb,
        )
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            message,
            context={"endpoint": endpoint, "method": verb},
        )
    )
    raise AssertionError("unreachable: raise_tool_error always raises")


async def _supervisor_api_call(
    client: HomeAssistantClient,
    endpoint: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Make a Supervisor API call through the supported install-mode transport.

    App (add-on) installs use their manager-role token against Supervisor REST.
    Other installs retain Home Assistant Core's ``supervisor/api`` WebSocket proxy.

    Args:
        client: Home Assistant client used for off-host WebSocket calls and
            as the TLS-verification source for direct REST.
        endpoint: Supervisor API endpoint (e.g., "/addons", "/addons/{slug}/info")
        method: HTTP method (default "GET")
        data: Optional request body data
        timeout: Optional timeout override

    A transient Supervisor job-group collision is retried while the
    ``_JOB_COLLISION_RETRY_WINDOW`` deadline remains. Individual transport
    attempts retain their normal timeout, so total elapsed time can exceed it.

    Returns:
        ``{"success": True, "result": ...}``. Every failure raises — this
        never returns an error dict.
    """
    try:
        kwargs: dict[str, Any] = {"endpoint": endpoint, "method": method}
        if data is not None:
            kwargs["data"] = data
        # On the WebSocket route, ``timeout`` tells the Supervisor proxy how
        # long to wait on the underlying REST operation. On the direct route,
        # only the local httpx timeout is needed. In both cases it must outlast
        # a multi-minute app operation; the default local wait is only 30s.
        wait_timeout = 30.0
        if timeout is not None:
            kwargs["timeout"] = timeout
            wait_timeout = float(timeout) + 15.0

        # Non-app deployments, including embedded mode, use the shared pooled
        # Home Assistant Core WebSocket (issue #1813).
        # App installs call Supervisor REST directly because its app-to-Core
        # proxy rejects app-originated ``supervisor/api`` commands.
        # Both transports feed the common retry and error-normalization path:
        # direct 4xx/5xx responses retain status metadata, while WebSocket
        # failures are classified from the returned message.
        #
        # Supervisor serialises jobs per app job group and rejects a
        # state-changing call while a still-settling job (a watchdog restart,
        # a prior start/stop, or a store reload) holds that group. The rejection
        # happens before the job body runs, so retrying cannot double-execute.
        # Each collision response checks the shared retry deadline before
        # backing off. Individual attempts retain their transport timeout, so
        # the final retry and total elapsed time may extend past that window.
        # Any other failure raises immediately.
        deadline = time.monotonic() + _JOB_COLLISION_RETRY_WINDOW
        delay = _JOB_COLLISION_RETRY_INITIAL_DELAY
        attempts = 0
        while True:
            attempts += 1
            result = await _supervisor_api_call_once(
                client,
                endpoint,
                method,
                data,
                wait_timeout,
                kwargs,
            )

            if result.get("success"):
                return {
                    "success": True,
                    "result": _supervisor_result_mapping(result, endpoint, method),
                }

            error_text = str(
                result.get("error", f"Supervisor API call failed: {endpoint}")
            )
            if _JOB_COLLISION_MARKER not in error_text.lower():
                _raise_supervisor_api_failure(result, endpoint)

            # The marker test runs on the raw text; everything below reports
            # it, so bind the size once the classification is settled.
            error_text = _bounded_supervisor_text(error_text)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # The retry budget is exhausted; the group may be stuck or
                # legitimately occupied by a long-running operation. Raise a
                # ToolError here so the caller gets guidance about the busy
                # job instead of the generic connectivity suggestion attached
                # to other failures.
                waited = _JOB_COLLISION_RETRY_WINDOW - remaining
                logger.warning(
                    "Supervisor job group still busy on %s after %.0fs "
                    "(%d attempts); giving up: %s",
                    endpoint,
                    waited,
                    attempts,
                    error_text,
                )
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        error_text,
                        context={
                            "endpoint": endpoint,
                            "attempts": attempts,
                            "waited_seconds": round(waited, 1),
                        },
                        suggestions=[
                            "Another job has held this app (add-on)'s job group for "
                            f"over {_JOB_COLLISION_RETRY_WINDOW:.0f}s — check "
                            "Supervisor logs for a stuck or long-running job",
                            "Retry once the in-flight app operation "
                            "(install, update, restart or backup) finishes",
                        ],
                    )
                )

            logger.info(
                "Supervisor job-group collision on %s; retrying in %.1fs (%s)",
                endpoint,
                delay,
                error_text,
            )
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 2, _JOB_COLLISION_RETRY_MAX_DELAY)

    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error calling Supervisor API {endpoint}: {e}")
        error_response = exception_to_structured_error(
            e,
            context={
                "endpoint": endpoint,
                "operation": f"Supervisor API {endpoint}",
                "timeout_seconds": wait_timeout,
            },
            raise_error=False,
        )
        error_details = error_response["error"]
        if error_details.get("code") == ErrorCode.RESOURCE_NOT_FOUND.value:
            error_details["suggestion"] = _SUPERVISOR_AVAILABILITY_SUGGESTION
            error_details["suggestions"] = [_SUPERVISOR_AVAILABILITY_SUGGESTION]
        error_details["suggestions"] = [
            *error_details.get("suggestions", []),
            "Check that this installation supports apps (add-ons).",
        ]
        raise_tool_error(error_response)
        return None  # unreachable: raise_tool_error always raises


async def _create_ingress_session(client: HomeAssistantClient) -> str:
    """Create a Supervisor ingress session and return its token.

    App mode uses a direct sibling-ingress route and never calls this helper.
    Non-app deployments, including embedded mode, mint sessions through Home
    Assistant Core's ``supervisor/api`` WebSocket command. The token is set as the
    ``ingress_session`` cookie on requests to Core's
    ``/api/hassio_ingress/<addon_token>/...`` endpoint, which Supervisor
    validates before proxying to the app container. Sessions are valid for
    approximately 15 minutes; a fresh one is minted per call.
    """
    response = await _supervisor_api_call(
        client, "/ingress/session", method="POST", data={}
    )

    session = response.get("result", {}).get("session")
    if not isinstance(session, str) or not session:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Supervisor returned no ingress session token",
                details=str(response),
            )
        )
    return session


async def _resolve_http_route(
    client: HomeAssistantClient,
    addon: dict[str, Any],
    normalized_path: str,
    port: int | None,
) -> tuple[str, dict[str, str]]:
    """Pick the HTTP route shape based on `port` and install variant.

    Three branches:
    - `port` set → direct container port (`http://<ip>:<port>/...`), no
      auth headers. Only reachable when the MCP host shares HA's container
      network.
    - Running as the HA app (add-on) (`is_running_in_addon()` true) → direct
      `<addon_ip>:<addon_ingress_port>` with `X-Ingress-Path` and
      `X-Hass-Source: core.ingress` headers. Routing through HA Core's
      `/api/hassio_ingress/...` proxy regresses here because
      `client.base_url` is `http://supervisor/core` (a Supervisor proxy
      mount that demands `Authorization: Bearer $SUPERVISOR_TOKEN`).
    - Off-host → HA Core ingress proxy at
      `<base_url>/api/hassio_ingress/<token>/<path>` with `Cookie:
      ingress_session=<token>`. Mints a fresh session per call.
    """
    addon_name = addon.get("name", "")
    headers: dict[str, str] = {}

    if port:
        addon_ip = addon.get("ip_address", "")
        if not addon_ip:
            raise_tool_error(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    f"App (add-on) '{addon_name}' is missing ip_address",
                    context={"slug": addon.get("slug"), "ip_address": addon_ip},
                )
            )
        return f"http://{addon_ip}:{port}/{normalized_path}", headers

    ingress_entry = addon.get("ingress_entry")
    if not ingress_entry:
        raise_tool_error(
            create_error_response(
                ErrorCode.INTERNAL_ERROR,
                f"App (add-on) '{addon_name}' is missing ingress_entry",
                context={"slug": addon.get("slug")},
            )
        )

    if is_running_in_addon():
        addon_ip = addon.get("ip_address", "")
        ingress_port = addon.get("ingress_port")
        if not addon_ip or not ingress_port:
            raise_tool_error(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    f"App (add-on) '{addon_name}' is missing network info "
                    "(ip_address or ingress_port)",
                    context={
                        "slug": addon.get("slug"),
                        "ip_address": addon_ip,
                        "ingress_port": ingress_port,
                    },
                )
            )
        # Sibling app (add-on) containers share the hassio bridge, so we hit the
        # ingress port directly. The X-Ingress-Path / X-Hass-Source headers
        # are what the app's nginx trusts as authenticated ingress source.
        headers["X-Ingress-Path"] = ingress_entry
        headers["X-Hass-Source"] = "core.ingress"
        return (
            f"http://{addon_ip}:{ingress_port}/{normalized_path}",
            headers,
        )

    session = await _create_ingress_session(client)
    base = client.base_url.rstrip("/")
    headers["Cookie"] = f"ingress_session={session}"
    return f"{base}{ingress_entry}/{normalized_path}", headers


async def _resolve_ws_route(
    client: HomeAssistantClient,
    addon: dict[str, Any],
    normalized_path: str,
    port: int | None,
) -> tuple[str, dict[str, str]]:
    """Pick the WebSocket route shape. Mirrors `_resolve_http_route`.

    The app-mode and direct-port branches always speak `ws://` because
    they hit the container directly. The off-host branch echoes
    `client.base_url`'s scheme (so HTTPS-fronted HA gets `wss://`).
    """
    addon_name = addon.get("name", "")
    headers: dict[str, str] = {}

    if port:
        addon_ip = addon.get("ip_address", "")
        if not addon_ip:
            raise_tool_error(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    f"App (add-on) '{addon_name}' is missing ip_address",
                    context={"slug": addon.get("slug")},
                )
            )
        return f"ws://{addon_ip}:{port}/{normalized_path}", headers

    ingress_entry = addon.get("ingress_entry")
    if not ingress_entry:
        raise_tool_error(
            create_error_response(
                ErrorCode.INTERNAL_ERROR,
                f"App (add-on) '{addon_name}' is missing ingress_entry",
                context={"slug": addon.get("slug")},
            )
        )

    if is_running_in_addon():
        addon_ip = addon.get("ip_address", "")
        ingress_port = addon.get("ingress_port")
        if not addon_ip or not ingress_port:
            raise_tool_error(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    f"App (add-on) '{addon_name}' is missing network info "
                    "(ip_address or ingress_port)",
                    context={
                        "slug": addon.get("slug"),
                        "ip_address": addon_ip,
                        "ingress_port": ingress_port,
                    },
                )
            )
        headers["X-Ingress-Path"] = ingress_entry
        headers["X-Hass-Source"] = "core.ingress"
        return (
            f"ws://{addon_ip}:{ingress_port}/{normalized_path}",
            headers,
        )

    session = await _create_ingress_session(client)
    parsed = urlsplit(client.base_url)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    ws_path_prefix = parsed.path.rstrip("/")
    headers["Cookie"] = f"ingress_session={session}"
    return (
        f"{ws_scheme}://{parsed.netloc}{ws_path_prefix}{ingress_entry}/{normalized_path}",
        headers,
    )


async def get_addon_info(client: HomeAssistantClient, slug: str) -> dict[str, Any]:
    """Get detailed info for a specific app (add-on).

    Args:
        client: Home Assistant REST client
        slug: App slug (e.g., "<prefix>_nodered")

    Returns:
        Dictionary with app details including ingress info, state, options, etc.
        Top-level ``log_level`` is surfaced when the app exposes one via its
        Supervisor options or schema (e.g., ``"debug"``, ``"info"``, etc.).
    """
    _validate_supervisor_slug(slug)
    response = await _supervisor_api_call(client, f"/addons/{slug}/info")

    return app_info_result(response["result"], slug, redact=redaction_enabled())


async def list_addons(
    client: HomeAssistantClient, include_stats: bool = False
) -> dict[str, Any]:
    """List installed Home Assistant apps (add-ons).

    Args:
        client: Home Assistant REST client
        include_stats: Include CPU/memory usage statistics

    Returns:
        Dictionary with installed apps and their status.
    """
    response = await _supervisor_api_call(client, "/addons")

    data = response["result"]
    addons = data.get("addons", [])

    # Fetch stats for running apps in parallel to avoid sequential overhead
    stats_by_slug: dict[str, dict[str, Any] | None] = {}
    stats_warnings: list[str] = []
    if include_stats:
        running_slugs = _running_addon_slugs(addons)

        async def _fetch_stats(
            slug: str,
        ) -> tuple[str, dict[str, Any] | None, str | None]:
            try:
                resp = await _supervisor_api_call(client, f"/addons/{slug}/stats")
                s = resp["result"]
                return (
                    slug,
                    {
                        "cpu_percent": s.get("cpu_percent"),
                        "memory_percent": s.get("memory_percent"),
                        "memory_usage": s.get("memory_usage"),
                        "memory_limit": s.get("memory_limit"),
                    },
                    None,
                )
            except ToolError as exc:
                warning = f"Statistics unavailable for app {slug!r}: {exc}"
                logger.warning("%s", warning)
                return slug, None, warning

        results = await asyncio.gather(*[_fetch_stats(slug) for slug in running_slugs])
        for slug, stats, warning in results:
            stats_by_slug[slug] = stats
            if warning is not None:
                stats_warnings.append(warning)

    return installed_apps_result(addons, include_stats, stats_by_slug, stats_warnings)


async def list_available_addons(
    client: HomeAssistantClient,
    repository: str | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    """List apps (add-ons) available in the app store.

    Args:
        client: Home Assistant REST client
        repository: Filter by repository slug (e.g., "core", "community")
        query: Search filter for app names/descriptions

    Returns:
        Dictionary with available apps and repositories.
    """
    response = await _supervisor_api_call(client, "/store")

    return available_apps_result(response["result"], repository, query)


def _validate_addon_access(
    addon: dict[str, Any],
    slug: str,
    addon_name: str,
    port: int | None,
    ingress_suggestions: list[str],
) -> None:
    """Raise a structured error if the app (add-on) is stopped or lacks Ingress."""
    if not port and not addon.get("ingress"):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_FAILED,
                f"App (add-on) '{addon_name}' does not support Ingress",
                suggestions=ingress_suggestions,
                context={"slug": slug},
            )
        )
    if addon.get("state") != "started":
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"App (add-on) '{addon_name}' is not running (state: {addon.get('state')})",
                suggestions=[
                    f"Start the app (add-on) first with: ha_manage_app(slug='{slug}', action='start')",
                ],
                context={"slug": slug, "state": addon.get("state")},
            )
        )


async def _collect_ws_messages_loop(
    ws: ClientConnection,
    collection_cap: int,
    timeout: int | float,
    wait_for_close: bool,
    caller_capped: bool,
    start_time: float,
) -> tuple[list[str], int, str]:
    """Collect messages from an open WebSocket until a stop condition is met."""
    collected: list[str] = []
    total_size = 0
    while True:
        remaining = timeout - (time.monotonic() - start_time)
        if remaining <= 0:
            return collected, total_size, "timeout"
        if len(collected) >= collection_cap:
            # Distinguish caller-set cap from the global safety ceiling so an
            # agent reading the response can tell "I capped this" from
            # "ha-mcp's hard ceiling kicked in".
            return (
                collected,
                total_size,
                "message_limit" if caller_capped else "safety_ceiling",
            )
        if total_size >= _MAX_RESPONSE_SIZE:
            return collected, total_size, "size_limit"
        recv_timeout = remaining if wait_for_close else min(remaining, 2.0)
        try:
            message = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
        except TimeoutError:
            return collected, total_size, "silence" if not wait_for_close else "timeout"
        except websockets.exceptions.ConnectionClosed:
            return collected, total_size, "server_closed"
        if isinstance(message, bytes):
            continue
        clean = ANSI_ESCAPE_RE.sub("", message)
        collected.append(clean)
        total_size += len(clean)


def _tls_setup_error(e: Exception, slug: str) -> NoReturn:
    """Report a CA-store/TLS-context build failure distinctly from network errors."""
    raise_tool_error(
        create_error_response(
            ErrorCode.CONNECTION_FAILED,
            f"Could not build a TLS context for the app (add-on) request: {e!s}",
            context={"slug": slug},
            suggestions=[
                "The MCP host's CA store failed to load. Check SSL_CERT_FILE "
                "and SSL_CERT_DIR, or the system certificate bundle.",
            ],
        )
    )


def _build_ws_ssl_context(
    ws_url: str, verify_ssl: bool, slug: str
) -> ssl.SSLContext | None:
    """Build the proxy's client TLS context, honoring the HA verify setting."""
    if not ws_url.startswith("wss://"):
        return None
    try:
        ssl_context = ssl.create_default_context()
    except OSError as e:
        _tls_setup_error(e, slug)
    if not verify_ssl:
        logger.warning(
            "TLS verification disabled for app (add-on) WebSocket proxy "
            "(HA_VERIFY_SSL=false). Connecting to %s with hostname/cert "
            "checks off.",
            ws_url,
        )
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    return ssl_context


def _tls_verification_failure_suggestions() -> list[str]:
    """Name the TLS remedy instead of pointing the caller at the network."""
    return [
        "Home Assistant's certificate did not validate. If it is self-signed "
        "or issued for a different hostname than the configured HA URL, set "
        "HA_VERIFY_SSL=false to skip verification, or reach HA at the "
        "hostname the certificate was issued for.",
    ]


def _build_addon_http_client(
    client: HomeAssistantClient, timeout: int, slug: str
) -> httpx.AsyncClient:
    """Build the proxy's HTTP client, honoring the HA verify setting.

    httpx builds its TLS context at construction; doing it before the request
    try reports a CA-store failure as such rather than letting it escape
    unstructured.
    """
    try:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            verify=client.verify_ssl,
        )
    except OSError as e:
        _tls_setup_error(e, slug)
        return None  # py/mixed-returns: unreachable, _tls_setup_error raises


def _raise_connect_failure(
    client: HomeAssistantClient,
    e: Exception,
    *,
    label: str,
    url: str,
    slug: str,
    port: int | None,
) -> NoReturn:
    """Raise the structured connect-phase error, classifying TLS failures.

    A certificate-verification failure with verification enabled gets the TLS
    remedy; everything else keeps the route-appropriate network guidance.
    """
    if _is_ssl_error(e) and client.verify_ssl:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONNECTION_FAILED,
                f"TLS verification failed connecting to {label}: {e!s}",
                details=f"url={url}",
                context={
                    "slug": slug,
                    "direct_port": bool(port),
                    "verify_ssl": True,
                },
                suggestions=_tls_verification_failure_suggestions(),
            )
        )
    raise_tool_error(
        create_error_response(
            ErrorCode.CONNECTION_FAILED,
            f"Failed to connect to {label}: {e!s}",
            details=f"url={url}",
            context={"slug": slug, "direct_port": bool(port)},
            suggestions=addon_connection_failure_suggestions(client, port, slug),
        )
    )


async def _run_ws_session(
    ws_url: str,
    headers: dict[str, str],
    body: dict[str, Any] | str | None,
    collection_cap: int,
    timeout: int,
    wait_for_close: bool,
    caller_capped: bool,
    ssl_context: ssl.SSLContext | None,
) -> tuple[list[str], int, str, float]:
    """Connect to a WebSocket URL, optionally send body, collect messages.

    Returns (collected, total_size, close_reason, elapsed_seconds).
    Exceptions from the WebSocket handshake or OS-level connect propagate to
    the caller, which maps them to structured ToolErrors.
    """
    start_time = time.monotonic()
    async with websockets.connect(
        ws_url,
        additional_headers=headers,
        ping_interval=20,
        ping_timeout=10,
        max_size=5 * 1024 * 1024,  # 5MB max per message
        open_timeout=10,
        close_timeout=5,
        ssl=ssl_context,
    ) as ws:
        if body is not None:
            await ws.send(json.dumps(body) if isinstance(body, dict) else str(body))
        collected, total_size, close_reason = await _collect_ws_messages_loop(
            ws, collection_cap, timeout, wait_for_close, caller_capped, start_time
        )
    return collected, total_size, close_reason, round(time.monotonic() - start_time, 2)


_build_ws_result = legacy_errors_sync(_app_proxy._build_ws_result)


def _ws_auth_error_suggestions(
    addon: dict[str, Any], slug: str, port: int | None, status: int
) -> list[str]:
    """Return route-appropriate guidance for a rejected WS handshake."""
    if port:
        # Prefer the caller-resolved slug; fall back to the app dictionary, then a
        # placeholder, so the suggestion never renders slug=''.
        slug_val = slug or addon.get("slug") or "<slug>"
        if _front_door_state(addon) is False:
            primary = _direct_port_auth_suggestion(slug_val)
        else:
            primary = _direct_port_rejection_suggestion()
    elif is_running_in_addon():
        # The app-mode route authenticates with Supervisor ingress
        # headers, not an ingress session or HA token — do not send the
        # caller to inspect credentials this request never carried.
        primary = (
            "The app (add-on) rejected the ingress-port handshake. This route "
            "authenticates with Supervisor ingress headers; check that the "
            "target app is running and its ingress is healthy."
        )
    else:
        primary = (
            "The ingress session may have expired or your HA token may lack the "
            "required scope. Verify the token has admin rights and try again."
        )
    return [
        primary,
        f"Status {status} from the WebSocket handshake.",
    ]


async def _call_addon_ws(
    client: HomeAssistantClient,
    slug: str,
    path: str,
    body: dict[str, Any] | str | None = None,
    timeout: int = 60,
    debug: bool = False,
    port: int | None = None,
    wait_for_close: bool = True,
    message_limit: int | None = None,
    message_offset: int = 0,
    summarize: bool = True,
    python_transform: str | None = None,
) -> dict[str, Any]:
    """Connect to an app (add-on)'s WebSocket API and collect messages.

    Routing mirrors the HTTP variant (see `_resolve_ws_route`): off-host
    ingress tunnels through HA Core's `/api/hassio_ingress` proxy; the
    HA app mode hits the container's ingress port directly;
    direct-port mode (`port` set) connects to the container's mapped port.

    Args:
        client: Home Assistant REST client
        slug: App slug (e.g., "<prefix>_esphome")
        path: WebSocket endpoint path (e.g., "/ws" for the ESPHome dashboard's command channel)
        body: Message to send after connecting (JSON-encoded if dict, raw if string)
        timeout: Max seconds to wait for messages (default 60)
        debug: Include diagnostic info
        port: Override port (same as HTTP tool)
        wait_for_close: If True, collect messages until server closes or timeout.
            If False, return after first batch of messages (up to 2s of silence).
        message_limit: Cap on messages collected from the wire. Bounded by the
            hard ceiling ``_MAX_WS_MESSAGES``. None means "collect up to the
            ceiling" (legacy behavior).
        message_offset: Drop this many messages from the start of the collected
            list before returning. Useful for paginating past a known-noisy
            header when re-running the same call.
        summarize: When True (default), collapse runs of non-signal messages
            (typically YAML config dumps) into short elision markers. Set to
            False to return the raw stream.
        python_transform: Optional sandboxed Python expression that post-
            processes the response. The variable ``response`` is bound to
            the list of parsed messages (``list[dict | str]``); the value
            of ``response`` after execution replaces ``messages`` in the
            output. Use ``ha_get_skill_guide`` for advanced transform patterns.

    Returns:
        Dictionary with collected messages, metadata, and status.
    """
    # 1. Sanitize path
    normalized = unquote(path).lstrip("/")
    if ".." in normalized.split("/"):
        raise_tool_error(
            create_validation_error(
                "Path contains '..' traversal component",
                parameter="path",
                details=f"Rejected path: {path}",
            )
        )

    # 2. Get app info and validate access
    addon_response = await get_addon_info(client, slug)
    if not addon_response.get("success"):
        raise_tool_error(addon_response)

    addon = addon_response["addon"]
    addon_name = addon.get("name", slug)
    _validate_addon_access(
        addon,
        slug,
        addon_name,
        port,
        ingress_suggestions=[
            "Use the 'port' parameter for WebSocket connections to this app (add-on)",
            f"Use ha_get_app(slug='{slug}') to see available ports",
        ],
    )

    # 3. Resolve route (direct-port / app-mode / off-host).
    ws_url, headers = await _resolve_ws_route(client, addon, normalized, port)

    # 4. Compute effective collection cap: callers may lower _MAX_WS_MESSAGES via
    # message_limit but cannot raise it. A caller's message_limit interacts
    # with message_offset — we collect enough to satisfy `offset + limit`
    # so requesting a later window actually returns the window.
    if message_limit is None:
        collection_cap = _MAX_WS_MESSAGES
    else:
        requested = max(0, message_offset) + max(0, message_limit)
        collection_cap = min(_MAX_WS_MESSAGES, requested)

    # Built before the try so a CA-store failure is reported as such rather
    # than as the app being unreachable.
    ssl_context = _build_ws_ssl_context(ws_url, client.verify_ssl, slug)
    try:
        collected, total_size, close_reason, elapsed = await _run_ws_session(
            ws_url,
            headers,
            body,
            collection_cap,
            timeout,
            wait_for_close,
            caller_capped=message_limit is not None,
            ssl_context=ssl_context,
        )
    except websockets.exceptions.InvalidHandshake as e:
        suggestions = [
            "Check that the app (add-on) supports WebSocket on this path",
            f"Use ha_get_app(slug='{slug}') to inspect available endpoints",
        ]
        # 401/403 means auth was rejected, not a path-shape problem.
        if isinstance(e, websockets.exceptions.InvalidStatus):
            status = e.response.status_code
            if status in (401, 403):
                suggestions = _ws_auth_error_suggestions(addon, slug, port, status)
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"WebSocket handshake failed with '{addon_name}': {e!s}",
                suggestions=suggestions,
                context={"slug": slug, "path": path},
            )
        )
    except websockets.exceptions.ConnectionClosed as e:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"WebSocket connection to '{addon_name}' closed unexpectedly: {e!s}",
                suggestions=[
                    "The app (add-on) may have rejected the connection or restarted",
                    "Try again or check app (add-on) logs for errors",
                ],
                context={"slug": slug, "path": path},
            )
        )
    except TimeoutError:
        raise_tool_error(
            create_error_response(
                ErrorCode.TIMEOUT_OPERATION,
                f"Operation 'WebSocket connection to {addon_name!r}' timed out after {timeout}s",
                details=f"path={path}",
                context={
                    "slug": slug,
                    "path": path,
                    "operation": f"WebSocket connection to '{addon_name}'",
                    "timeout_seconds": timeout,
                    "direct_port": bool(port),
                },
                suggestions=addon_connection_failure_suggestions(
                    client, port, slug, timed_out=True
                ),
            )
        )
    except OSError as e:
        _raise_connect_failure(
            client,
            e,
            label=f"app (add-on) '{addon_name}' WebSocket",
            url=ws_url,
            slug=slug,
            port=port,
        )

    result: dict[str, Any] = _build_ws_result(
        slug=slug,
        addon_name=addon_name,
        collected=collected,
        close_reason=close_reason,
        elapsed=elapsed,
        message_limit=message_limit,
        message_offset=message_offset,
        summarize=summarize,
        python_transform=python_transform,
        debug=debug,
        ws_url=ws_url,
        headers=headers,
        body=body,
        total_size=total_size,
        collection_cap=collection_cap,
    )
    return result


def _parse_response_body(response: httpx.Response) -> Any:
    """Parse HTTP response body: JSON if content-type matches, else raw text."""
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError):
            return response.text
    return response.text


_truncate_http_response = _app_proxy._truncate_http_response


def _build_http_result(
    response: httpx.Response,
    response_data: Any,
    addon_name: str,
    slug: str,
    url: str,
    headers: dict[str, str],
    debug: bool,
    pagination_meta: dict[str, Any] | None,
    transformed: bool,
    truncated: bool,
) -> dict[str, Any]:
    return _app_proxy.http_result(
        response.status_code,
        dict(response.headers),
        response_data,
        addon_name,
        slug,
        url,
        headers,
        debug,
        pagination_meta,
        transformed,
        truncated,
    )


def _add_http_error_hints(
    result: dict[str, Any],
    response: httpx.Response,
    addon: dict[str, Any],
    slug: str,
    direct_port: bool,
) -> None:
    add_http_error_hints(result, response.status_code, addon, slug, direct_port)


async def _call_addon_api(
    client: HomeAssistantClient,
    slug: str,
    path: str,
    method: str = "GET",
    body: dict[str, Any] | list[Any] | str | None = None,
    timeout: int = 30,
    debug: bool = False,
    port: int | None = None,
    offset: int = 0,
    limit: int | None = None,
    python_transform: str | None = None,
    raw: bool = False,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Call an app (add-on)'s web API.

    Routing is picked per install variant (see `_resolve_http_route`):

    - **Ingress (default), off-host**: tunnels through HA Core's
      `/api/hassio_ingress/<token>/...` proxy with a per-call Supervisor
      session cookie. The path that makes off-host (PyPI/uvx) installs work.
    - **Ingress (default), HA app**: hits the app container's
      ingress port directly with the `core.ingress` source headers. Avoids
      the Supervisor `/core` proxy hop that would otherwise demand
      `Authorization: Bearer $SUPERVISOR_TOKEN` on top of the cookie.
    - **Direct port** (when `port` is set): connects to
      `http://<addon_ip>:<port>/...` for apps that expose mapped ports
      (e.g. Node-RED on 1880). Only works when the MCP host shares HA's
      Docker network.

    Args:
        client: Home Assistant REST client
        slug: App slug (e.g., "<prefix>_nodered")
        path: API path relative to app root (e.g., "/flows")
        method: HTTP method (GET, POST, PUT, DELETE, PATCH)
        body: Request body for POST/PUT/PATCH (dict, list, or pre-encoded JSON string)
        timeout: Request timeout in seconds (default 30)
        port: Override port to connect to (e.g., direct access port instead of ingress port)
        offset: Skip this many items in array responses (default 0)
        limit: Return at most this many items from array responses
        python_transform: Optional sandboxed Python expression applied to the
            parsed response body. The variable ``response`` is bound to
            ``dict | list | str`` depending on content-type. Transform runs
            after offset/limit slicing.
        raw: Internal flag — when True, skip the size-based truncation that
            otherwise replaces large array/object responses with an error
            placeholder. Used by array_patch mode in ha_manage_app, which
            needs the full parsed response in memory to apply operations
            even when the JSON is larger than _MAX_RESPONSE_SIZE.
        extra_headers: Optional caller-supplied request headers. Ingress route
            headers override matching caller values; direct-port routes add no
            internal headers. When a body is supplied, its derived
            `Content-Type` overrides a caller value. Use this for app API
            requirements like Node-RED's `Node-RED-Deployment-Type` header.
    """
    # 1. Sanitize path to prevent traversal attacks (including URL-encoded)
    normalized = unquote(path).lstrip("/")
    if ".." in normalized.split("/"):
        raise_tool_error(
            create_validation_error(
                "Path contains '..' traversal component",
                parameter="path",
                details=f"Rejected path: {path}",
            )
        )

    # 2. Get app info and validate access
    addon_response = await get_addon_info(client, slug)
    if not addon_response.get("success"):
        raise_tool_error(addon_response)

    addon = addon_response["addon"]
    addon_name = addon.get("name", slug)
    _validate_addon_access(
        addon,
        slug,
        addon_name,
        port,
        ingress_suggestions=[
            "Check if this app (add-on) exposes a direct port instead",
            f"Use ha_get_app(slug='{slug}') to see port mappings",
            "Use the 'port' parameter to connect to a direct access port",
        ],
    )

    # 3. Resolve route (direct-port / app-mode / off-host).
    url, headers = await _resolve_http_route(client, addon, normalized, port)

    # 4. Layer caller headers under any ingress routing headers. Direct-port
    # routes have no internal headers; ingress route identity always wins on
    # collision. Content-Type is derived from the body below, when present.
    if extra_headers:
        merged = dict(extra_headers)
        merged.update(headers)
        headers = merged

    # 5. Set content type based on body type
    if isinstance(body, dict | list):
        headers["Content-Type"] = "application/json"
        request_content = json.dumps(body).encode()
    elif isinstance(body, str):
        headers["Content-Type"] = "application/json"
        request_content = body.encode()
    else:
        request_content = None

    addon_http_client = _build_addon_http_client(client, timeout, slug)
    try:
        async with addon_http_client as http_client:
            response = await http_client.request(
                method=method.upper(),
                url=url,
                headers=headers,
                content=request_content,
            )
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        _raise_connect_failure(
            client,
            e,
            label=f"app (add-on) '{addon_name}'",
            url=url,
            slug=slug,
            port=port,
        )
    except httpx.TimeoutException:
        raise_tool_error(
            create_error_response(
                ErrorCode.TIMEOUT_OPERATION,
                f"Operation 'app (add-on) API call to {addon_name!r}' timed out after {timeout}s",
                details=f"path={path}, method={method}",
                context={
                    "slug": slug,
                    "path": path,
                    "operation": f"app (add-on) API call to '{addon_name}'",
                    "timeout_seconds": timeout,
                    "direct_port": bool(port),
                },
                suggestions=addon_connection_failure_suggestions(
                    client, port, slug, timed_out=True
                ),
            )
        )
    # 6. Parse response body
    response_data: Any = _parse_response_body(response)

    # 7. Apply offset/limit slicing to array responses
    pagination_meta: dict[str, Any] | None = None
    if isinstance(response_data, list) and (offset > 0 or limit is not None):
        total_items = len(response_data)
        end = offset + limit if limit is not None else total_items
        response_data = response_data[offset:end]
        pagination_meta = {
            "total_items": total_items,
            "offset": offset,
            "limit": limit,
            "returned": len(response_data),
        }

    # 8. python_transform (optional) — runs after slicing, before size cap,
    # so an agent can narrow a large response down under the limit.
    transformed = False
    if python_transform is not None:
        response_data = _apply_response_transform(response_data, python_transform)
        transformed = True

    # 9. Truncate large responses (skipped in raw mode)
    response_data, truncated = _truncate_http_response(response_data, raw)

    result = _build_http_result(
        response,
        response_data,
        addon_name,
        slug,
        url,
        headers,
        debug,
        pagination_meta,
        transformed,
        truncated,
    )
    _add_http_error_hints(result, response, addon, slug, direct_port=bool(port))
    return result


class AddOnTools(AppTools, AppDispatch):
    """Encapsulates app (add-on) management logic for ha_get_app and ha_manage_app.

    ha_manage_app supports five mutually exclusive modes: lifecycle
    (install/start/stop/restart/rebuild/update/uninstall), store-repository
    (add_repository/remove_repository), config
    (options/network/boot/auto_update/watchdog), proxy (path-based HTTP or
    WebSocket), and array-patch (fetch-modify-post on a JSON array endpoint).
    """

    def __init__(self, client: HomeAssistantClient) -> None:
        self._client = client
        super().__init__(apps=self)

    async def get_addon(
        self,
        source: Literal["installed", "available"] | None,
        slug: str | None,
        include_stats: bool,
        repository: str | None,
        query: str | None,
    ) -> dict[str, Any]:
        if slug:
            return await get_addon_info(self._client, slug)

        effective_source = (source or "installed").lower()

        if effective_source == "available":
            result = await list_available_addons(self._client, repository, query)
        elif effective_source == "installed":
            result = await list_addons(self._client, include_stats)
        else:
            raise_tool_error(
                create_validation_error(
                    f"Invalid source: {source}. Must be 'installed' or 'available'.",
                    parameter="source",
                    details="Valid sources: installed, available",
                )
            )

        return result

    _build_config_payload = staticmethod(
        legacy_errors_sync(AppDispatch._build_config_payload)
    )

    _validate_manage_mode = staticmethod(
        legacy_errors_sync(AppDispatch._validate_manage_mode)
    )

    # Supervisor lifecycle endpoints. install/update live under /store; the
    # rest under /addons. install/rebuild build a local image and can be slow,
    # so they get a generous timeout.
    _ACTION_ENDPOINTS: ClassVar[dict[str, tuple[str, int]]] = {
        "install": ("/store/addons/{slug}/install", 1800),
        "update": ("/store/addons/{slug}/update", 1800),
        "rebuild": ("/addons/{slug}/rebuild", 1800),
        "start": ("/addons/{slug}/start", 120),
        "stop": ("/addons/{slug}/stop", 60),
        "restart": ("/addons/{slug}/restart", 120),
        "uninstall": ("/addons/{slug}/uninstall", 120),
    }

    # Store-repository actions operate on the store, not an installed app (add-on),
    # so they take a repository URL/slug via the `repository` param instead of
    # `slug`. add can clone a remote git repo (network-bound), so it gets a
    # generous timeout.
    _REPOSITORY_ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {"add_repository", "remove_repository"}
    )

    # Store-wide actions take neither `slug` nor `repository`: they act on the
    # whole store. `check_updates` mirrors the Home Assistant Apps UI overflow
    # item of the same name (frontend `check_updates` ->
    # `POST /store/reload`), which re-scans local app (add-on) sources on
    # demand after their config.yaml changes. Supervisor also reloads the
    # store on its own every three hours (misc/tasks.py RUN_RELOAD_APPS), so
    # this buys promptness, not a capability nothing else has.
    _STORE_ACTIONS: ClassVar[frozenset[str]] = frozenset({"check_updates"})

    # A reload re-reads every registered repository, including git pulls of
    # remote ones, so it needs more than the 60-120s lifecycle timeouts. It
    # builds no image, so it does not need the 1800s install/update budget.
    _CHECK_UPDATES_TIMEOUT: ClassVar[int] = 300

    async def _reject_self_update_in_addon(self, slug: str) -> None:
        """Reject only the direct Supervisor update that would update this app.

        Supervisor identifies direct requests by app token and forbids an app
        from updating itself. Its Core proxy also blocks privileged
        ``supervisor/*`` and ``hassio/*`` WebSocket commands from apps, so
        there is no safe programmatic fallback from this process.
        """
        response = await _supervisor_api_call(self._client, "/addons/self/info")
        self_info = response.get("result")
        self_slug = self_info.get("slug") if isinstance(self_info, dict) else None
        if not isinstance(self_slug, str) or not self_slug:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Supervisor returned no app identity; refusing an update "
                    "that could target the running ha-mcp app.",
                    context={"slug": slug, "endpoint": "/addons/self/info"},
                    suggestions=[
                        "Use the Home Assistant Apps UI to update the target app",
                        "Check Supervisor logs for the missing self app identity",
                    ],
                )
            )
        if slug != self_slug:
            return
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"App (add-on) {slug} cannot update itself while ha-mcp is "
                "running inside that app.",
                context={"slug": slug, "self_slug": self_slug},
                suggestions=[
                    "Update the ha-mcp app from the Home Assistant Apps UI",
                    "After ha-mcp restarts, verify its version with ha_get_app",
                ],
            )
        )

    async def _execute_action_mode(self, slug: str, action: str) -> dict[str, Any]:
        """Run a Supervisor app (add-on) lifecycle action (install/start/stop/etc.).

        Powers the "install the engine for the user" flow: an LLM can install
        an app from a registered store repository and start it, rather than
        only updating config or proxying to an already-running app.
        """
        key = action.lower().strip()
        endpoint_tmpl, timeout = self._ACTION_ENDPOINTS.get(key, (None, 0))
        if endpoint_tmpl is None:
            raise_tool_error(
                create_validation_error(
                    f"Invalid action: {action!r}. Must be one of: "
                    f"{', '.join(sorted(self._ACTION_ENDPOINTS))}.",
                    parameter="action",
                )
            )
        endpoint = endpoint_tmpl.format(slug=slug)
        if key == "update" and is_running_in_addon():
            await self._reject_self_update_in_addon(slug)
        await _supervisor_api_call(
            self._client, endpoint, method="POST", timeout=timeout
        )
        return {
            "success": True,
            "action": key,
            "slug": slug,
            "message": f"App (add-on) {slug} {key} completed.",
        }

    async def _store_latest_versions(self) -> dict[str, dict[str, Any]]:
        """Snapshot the store's latest-known version per app (add-on) slug.

        Supervisor answers ``POST /store/reload`` with a bare ``ok``, so the
        only way to report what a reload actually discovered is to compare the
        store before and after.

        Reads ``version_latest`` (Supervisor's ``app.latest_version``), never
        ``version``: on a ``/store`` entry ``version`` is the *installed*
        version and is ``None`` for anything not installed, so diffing it
        would miss the newly published release a reload exists to find.
        """
        response = await _supervisor_api_call(self._client, "/store")
        snapshot: dict[str, dict[str, Any]] = store_versions(response.get("result"))
        return snapshot

    _diff_store_versions = staticmethod(diff_store_versions)

    async def _execute_check_updates(self) -> dict[str, Any]:
        """Reload the app (add-on) store so Supervisor re-scans its sources.

        This is the programmatic form of the Apps UI overflow item "Check for
        updates". It refreshes *available* metadata only and installs nothing;
        a caller that wants the new version on disk still has to follow with
        ``action="update"`` (or ``action="rebuild"`` for a local app whose
        source changed underneath an unchanged version).

        ``changed`` and ``updates_available`` describe *store* metadata, not
        anything on disk: a slug listed here still runs its old version until
        the caller installs it. Either field is ``None`` when the store could
        not be read to measure it — never ``[]``, which would claim nothing
        was found.
        """
        warnings: list[str] = []
        before = await self._snapshot_store_or_warn(warnings, "before")
        try:
            await _supervisor_api_call(
                self._client,
                "/store/reload",
                method="POST",
                timeout=self._CHECK_UPDATES_TIMEOUT,
            )
        except ToolError as error:
            self._raise_check_updates_error(error)
        after = await self._snapshot_store_or_warn(warnings, "after")

        return store_refresh_result(before, after, warnings)

    async def _snapshot_store_or_warn(
        self, warnings: list[str], when: str
    ) -> dict[str, dict[str, Any]] | None:
        """Snapshot the store, degrading to ``None`` with a warning.

        The reload is the operation the caller asked for. A store read that
        fails around it costs detail in the result, so it must not turn a
        completed reload into a tool-level failure. It returns ``None`` rather
        than an empty map so the caller can tell "no apps" from "could not
        look".
        """
        try:
            return await self._store_latest_versions()
        except ToolError as error:
            detail = AddOnTools._supervisor_error_text(str(error))
            logger.warning(
                "Could not read the app (add-on) store %s the reload: %s",
                when,
                detail,
            )
            # Carry the cause: without it an auth failure and an unreachable
            # Supervisor read identically in the result.
            warnings.append(
                f"Could not read the app (add-on) store {when} the reload: {detail}"
            )
            return None

    @staticmethod
    def _raise_check_updates_error(error: ToolError) -> NoReturn:
        """Re-raise a failed store reload with guidance for its known causes."""
        detail = AddOnTools._supervisor_error_text(str(error))
        # Supervisor's own wording for this refusal (jobs/decorator.py):
        # "'<job>' blocked from execution, supervisor needs to be updated
        # first". Matching on "supervisor" + "update" anywhere would relabel
        # unrelated app-update failures with this guidance.
        if "needs to be updated first" in detail.lower():
            # Built outside the list: an implicit concatenation inside a list
            # literal reads as a missing comma (py/implicit-string-concatenation-in-list).
            retry_suggestion = (
                "Update Supervisor first, then retry "
                "ha_manage_app(action='check_updates')"
            )
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Supervisor refused to reload the app (add-on) store "
                    "because Supervisor itself has a pending update.",
                    details=detail,
                    suggestions=[
                        retry_suggestion,
                        "Check Supervisor status with ha_get_system_health",
                    ],
                )
            )
        raise error

    async def _execute_repository_action(
        self, action: str, repository: str
    ) -> dict[str, Any]:
        """Add or remove a Supervisor app (add-on) store repository.

        ``add_repository`` registers a custom app repository by URL
        (``POST /store/repositories`` with body ``{"repository": "<url>"}``);
        ``remove_repository`` unregisters one by its repository slug
        (``DELETE /store/repositories/{slug}``). Registering a repository is
        what makes its apps show up in ``ha_get_app(source="available")``
        so they can then be installed via lifecycle ``action="install"``.
        """
        key = action.lower().strip()
        # add clones a remote git repo (network-bound); both operations can take
        # a little time, so give them a reasonable timeout. _supervisor_api_call
        # couples the local await to timeout+15.
        timeout = 120
        if key == "add_repository":
            endpoint = "/store/repositories"
            method = "POST"
            data: dict[str, Any] | None = {"repository": repository}
        else:  # remove_repository
            endpoint = f"/store/repositories/{repository}"
            method = "DELETE"
            data = None
        # Make the actions idempotent: adding a repo Supervisor already has
        # ("already in the store") or removing one it doesn't have are both the
        # desired end state, so report success instead of a confusing error (the
        # "add repo then install" flow re-adds freely). _supervisor_api_call raises
        # a ToolError on every failure.
        try:
            await _supervisor_api_call(
                self._client, endpoint, method=method, data=data, timeout=timeout
            )
        except ToolError as error:
            return self._repo_noop_or_raise(key, repository, error)
        return {
            "success": True,
            "action": key,
            "repository": repository,
            "message": f"Repository {repository} {key} completed.",
        }

    def _repo_noop_or_raise(
        self, key: str, repository: str, error: ToolError
    ) -> dict[str, Any]:
        """Reclassify an idempotent no-op failure as success, else raise.

        Logs the reclassification so a failure that gets demoted to a success
        is never invisible."""
        error_text = str(error)
        if self._structured_error_outcome(error_text) == "unknown":
            raise error
        error_code = self._structured_error_code(error_text)
        noop_codes = {None, ErrorCode.SERVICE_CALL_FAILED.value}
        if key == "remove_repository":
            noop_codes.add(ErrorCode.RESOURCE_NOT_FOUND.value)
        if error_code not in noop_codes:
            raise error

        noop = self._repo_noop_verb(key, self._supervisor_error_text(error_text))
        if noop:
            logger.info(
                "Treating %s of repository %r as an idempotent no-op (%s).",
                key,
                repository,
                noop,
            )
            return self._repo_noop_result(key, repository, noop)
        if error_code != ErrorCode.SERVICE_CALL_FAILED.value:
            raise error
        self._raise_repo_action_error(key, repository, error_text)
        raise AssertionError("unreachable: _raise_repo_action_error always raises")

    @staticmethod
    def _structured_error_code(error_text: str) -> str | None:
        """Return the code from a serialized structured ToolError."""
        try:
            payload = json.loads(error_text)
        except (ValueError, TypeError):
            return None
        err = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(err, dict):
            return None
        code = err.get("code")
        return code if isinstance(code, str) else None

    @staticmethod
    def _structured_error_outcome(error_text: str) -> str | None:
        """Return the write outcome from a serialized structured ToolError."""
        try:
            payload = json.loads(error_text)
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        outcome = payload.get("outcome")
        return outcome if isinstance(outcome, str) else None

    @staticmethod
    def _supervisor_error_text(error_text: str) -> str:
        """Extract just the Supervisor-reported error from a serialized failure.

        Structured failures may carry Supervisor text in ``details`` after
        WebSocket/error normalization or directly in ``message`` for REST and
        command failures. Return ``details`` first, falling back to ``message``
        or the raw text, so idempotency matching considers only the real cause
        rather than other serialized error metadata."""
        try:
            payload = json.loads(error_text)
        except (ValueError, TypeError):
            return error_text
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            return str(err.get("details") or err.get("message") or error_text)
        return error_text

    @staticmethod
    def _repo_noop_verb(key: str, error_text: str) -> str | None:
        """Return a status word if a repo-action failure means the desired end
        state already holds (an idempotent no-op), else None.

        Scoped tightly so an unrelated failure that merely happens to mention
        "not found" somewhere (a dependent app (add-on), a misrouted 404, a file
        path) is NOT silently reclassified as success: the not-found phrasing
        must be about a repository."""
        text = error_text.lower()
        if key == "add_repository" and "already in the store" in text:
            return "already registered"
        if (
            key == "remove_repository"
            and "repositor" in text
            and ("not found" in text or "does not exist" in text)
        ):
            return "not registered"
        return None

    @staticmethod
    def _repo_noop_result(key: str, repository: str, verb: str) -> dict[str, Any]:
        return {
            "success": True,
            "action": key,
            "repository": repository,
            "message": f"Repository {repository} is {verb}; no change needed.",
        }

    @staticmethod
    def _raise_repo_action_error(key: str, repository: str, detail: str) -> NoReturn:
        """Raise a repository-action-specific error.

        Only ``SERVICE_CALL_FAILED`` domain errors reach this helper. Replace
        their generic connectivity suggestion with actionable guidance for an
        invalid repository URL or a repository still used by installed apps.
        """
        if key == "add_repository":
            suggestions = [
                "Verify the repository is a valid Home Assistant app (add-on) "
                "repository URL, e.g. https://github.com/<owner>/<repo>",
            ]
        else:
            suggestions = [
                "Verify the repository slug — list current repositories with "
                + "ha_get_app(source='available')",
                "A repository that still has installed apps (add-ons) can't be removed "
                + "until those apps are uninstalled",
            ]
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Could not {key.replace('_', ' ')} {repository!r}.",
                details=detail,
                suggestions=suggestions,
            )
        )

    _reject_sentinel_options = staticmethod(
        legacy_errors_sync(AppDispatch._reject_sentinel_options)
    )

    async def _execute_config_mode(
        self,
        slug: str,
        config_data: dict[str, Any],
    ) -> dict[str, Any]:
        ignored_fields: list[str] = []
        if "options" in config_data:
            self._reject_sentinel_options(config_data["options"])
            info_result = await _supervisor_api_call(
                self._client, f"/addons/{slug}/info"
            )
            addon_info = info_result.get("result", {})

            # Merge caller's options into current options (fixes partial-update
            # rejection). Supervisor validates the full options dict against the
            # app (add-on) schema, so callers must always submit all required fields —
            # merging makes that transparent.
            current_options: dict = addon_info.get("options") or {}
            merged_options = _merge_options(current_options, config_data["options"])

            # Both the live current options and the merged result are
            # password-bearing; remember them for the global known-value
            # scrub (this path fetches info directly, bypassing
            # get_addon_info's harvesting). The merged copy carries any
            # password the caller is submitting right now — without it a
            # freshly written secret stays unknown to the scrub until some
            # later read harvests it.
            if redaction_enabled() and isinstance(addon_info.get("schema"), list):
                register_known_secret_values(
                    collect_addon_secret_values(current_options, addon_info["schema"])
                    | collect_addon_secret_values(merged_options, addon_info["schema"])
                )

            # Pre-write schema check: identify fields not in the app's schema.
            # Supervisor silently drops unknown fields on write; surfacing them
            # here lets the caller correct mistakes before any state is changed.
            schema_ui: list | None = addon_info.get("schema")
            if schema_ui is not None:
                allowed_keys = {item["name"] for item in schema_ui if "name" in item}
                ignored_fields = [
                    k for k in config_data["options"] if k not in allowed_keys
                ]
                for k in ignored_fields:
                    merged_options.pop(k, None)

            config_data["options"] = merged_options

        await _supervisor_api_call(
            self._client,
            f"/addons/{slug}/options",
            method="POST",
            data=config_data,
        )
        return config_submitted_result(slug, config_data, ignored_fields)

    _validate_array_patch_input = staticmethod(
        legacy_errors_sync(AppDispatch._validate_array_patch_input)
    )

    async def _execute_array_patch(
        self,
        slug: str,
        path: str,
        array_patch: dict[str, Any],
        websocket: bool,
        body: Any,
        offset: int,
        limit: int | None,
        debug: bool,
        port: int | None,
        request_headers: dict[str, str] | None,
    ) -> dict[str, Any]:
        id_field, ops = self._validate_array_patch_input(
            array_patch, websocket, body, offset, limit
        )

        fetch_result = await _call_addon_api(
            client=self._client,
            slug=slug,
            path=path,
            method="GET",
            debug=debug,
            port=port,
            raw=True,
            extra_headers=request_headers,
        )
        if not fetch_result.get("success"):
            raise_tool_error(fetch_result)

        fetched = fetch_result.get("response")
        if not isinstance(fetched, list):
            raise_tool_error(
                create_validation_error(
                    f"array_patch requires a JSON array at {path!r}; "
                    f"got {type(fetched).__name__}",
                    parameter="path",
                )
            )

        new_array, summary = _apply_array_ops(fetched, ops, id_field)

        post_result = await _call_addon_api(
            client=self._client,
            slug=slug,
            path=path,
            method="POST",
            body=new_array,
            debug=debug,
            port=port,
            extra_headers=request_headers,
        )
        if not post_result.get("success"):
            raise_tool_error(post_result)

        response_payload: dict[str, Any] = {
            "success": True,
            "slug": slug,
            "addon_name": fetch_result.get("addon_name"),
            "path": path,
            "id_field": id_field,
            "items_before": len(fetched),
            "items_after": len(new_array),
            "summary": summary,
        }
        if debug:
            response_payload["_debug"] = {
                "fetch": fetch_result.get("_debug"),
                "post": post_result.get("_debug"),
            }
        return response_payload

    _proxy_overrides_basic = staticmethod(
        legacy_errors_sync(AppDispatch._proxy_overrides_basic)
    )

    _proxy_overrides_ws_and_extra = staticmethod(
        legacy_errors_sync(AppDispatch._proxy_overrides_ws_and_extra)
    )

    _dispatch_repository_action = legacy_errors(AppDispatch._dispatch_repository_action)

    _reject_store_action_conflicts = staticmethod(
        legacy_errors_sync(AppDispatch._reject_store_action_conflicts)
    )

    _reject_action_mode_conflicts = staticmethod(
        legacy_errors_sync(AppDispatch._reject_action_mode_conflicts)
    )

    _reject_config_mode_proxy_params = legacy_errors_sync(
        AppDispatch._reject_config_mode_proxy_params
    )

    async def _execute_ws_proxy(
        self,
        slug: str,
        path: str,
        body: dict[str, Any] | str | None,
        debug: bool,
        port: int | None,
        wait_for_close: bool,
        message_limit: int | None,
        message_offset: int,
        summarize: bool,
        python_transform: str | None,
    ) -> dict[str, Any]:
        result = await _call_addon_ws(
            client=self._client,
            slug=slug,
            path=path,
            body=body,
            timeout=120 if wait_for_close else 10,
            debug=debug,
            port=port,
            wait_for_close=wait_for_close,
            message_limit=message_limit,
            message_offset=message_offset,
            summarize=summarize,
            python_transform=python_transform,
        )
        if not result.get("success"):
            raise_tool_error(result)
        return result

    async def _execute_http_proxy(
        self,
        *,
        slug: str,
        path: str,
        method: str,
        body: dict[str, Any] | str | None,
        debug: bool,
        port: int | None,
        offset: int,
        limit: int | None,
        python_transform: str | None,
        request_headers: dict[str, str] | None,
        message_limit: int | None,
        message_offset: int,
        summarize: bool,
    ) -> dict[str, Any]:
        valid_methods = {"GET", "POST", "PUT", "DELETE", "PATCH"}
        if method.upper() not in valid_methods:
            raise_tool_error(
                create_validation_error(
                    f"Invalid HTTP method: {method}. Must be one of: {', '.join(sorted(valid_methods))}",
                    parameter="method",
                )
            )
        if message_limit is not None or message_offset != 0 or not summarize:
            raise_tool_error(
                create_validation_error(
                    "message_limit / message_offset / summarize apply only to "
                    "WebSocket mode. Set websocket=True or remove them.",
                    parameter="message_limit",
                )
            )

        result = await _call_addon_api(
            client=self._client,
            slug=slug,
            path=path,
            method=method,
            body=body,
            debug=debug,
            port=port,
            offset=offset,
            limit=limit,
            python_transform=python_transform,
            extra_headers=request_headers,
        )
        if not result.get("success"):
            raise_tool_error(result)
        return result

    manage_addon = legacy_errors(AppDispatch.manage_addon)

    ha_get_app = tool(
        name="ha_get_app",
        tags={"Apps (add-ons)"},
        annotations=read_only_hints("Get Apps (add-ons)", open_world=True),
    )(log_tool_usage(legacy_errors(AppTools.ha_get_app)))

    ha_manage_app = tool(
        name="ha_manage_app",
        tags={"Apps (add-ons)"},
        annotations=write_hints(
            "Manage App (add-on)", destructive=True, idempotent=False, open_world=True
        ),
    )(log_tool_usage(legacy_errors(AppTools.ha_manage_app)))


# User-facing prose uses app (add-on) on first mention and app thereafter.
# Retain legacy spelling only in concrete identifiers and API paths such as
# /addons, app slugs, and existing symbol names.
def register_addon_tools(mcp: Any, client: HomeAssistantClient, **kwargs: Any) -> None:
    """Register the public app contracts against the legacy connection owner."""
    register_tool_methods(mcp, AddOnTools(client))


__all__ = [
    "AddOnTools",
    "_extract_addon_log_level",
    "_is_signal_message",
    "_slice_ws_messages",
    "_summarize_ws_messages",
    "get_addon_info",
    "list_addons",
    "list_available_addons",
    "register_addon_tools",
]
