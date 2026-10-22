"""Stable Lovelace render-path resolution for dashboard screenshots."""

from __future__ import annotations

import json
from typing import Any, cast

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backends.legacy import legacy_errors_sync
from ha_mcp.runtime import dashboard_routes as _routes
from ha_mcp.runtime import screenshot_routes as _screenshots
from ha_mcp.runtime.errors import raise_failure
from ha_mcp.tool_api import ToolFailure

from ..errors import ErrorCode, create_error_response
from ..tools.helpers import raise_tool_error

DashboardRenderTarget = _routes.DashboardRenderTarget
dashboard_frontend_path = legacy_errors_sync(_routes.dashboard_frontend_path)
_configured_view_path = legacy_errors_sync(_routes._configured_view_path)
_numeric_view_index = legacy_errors_sync(_routes._numeric_view_index)
_view_path_requires_numeric_fallback = legacy_errors_sync(
    _routes._view_path_requires_numeric_fallback
)
_numeric_fallback_suffix = legacy_errors_sync(_routes._numeric_fallback_suffix)
_raw_frontend_view_match = legacy_errors_sync(_routes._raw_frontend_view_match)
_render_path_metadata = legacy_errors_sync(_routes._render_path_metadata)
dashboard_render_paths = legacy_errors_sync(_routes.dashboard_render_paths)
_safe_named_render_path = legacy_errors_sync(_routes._safe_named_render_path)
_fallback_view_target = legacy_errors_sync(_routes._fallback_view_target)
match_dashboard_view = legacy_errors_sync(_routes.match_dashboard_view)
resolve_dashboard_view = legacy_errors_sync(_routes.resolve_dashboard_view)
_JS_DECIMAL_NUMBER = _routes._JS_DECIMAL_NUMBER
_JS_RADIX_NUMBER = _routes._JS_RADIX_NUMBER
_RESERVED_VIEW_PATHS = _routes._RESERVED_VIEW_PATHS
_DECODE_URI_RESERVED_VIEW_CHARS = _routes._DECODE_URI_RESERVED_VIEW_CHARS


_UNKNOWN_CONFIG_PREFIX = "Unknown config specified:"
_NO_CONFIG_MESSAGES = frozenset(
    {"No config found.", "Command failed: No config found."}
)
# Puppet percent-encodes each render-path segment, while HA's frontend applies
# decodeURI before matching views.  decodeURI intentionally leaves these URI
# delimiters encoded, so an exact configured-path match would silently miss.


async def fetch_dashboard_render_config(client: Any, url_path: str) -> dict[str, Any]:
    """Fetch a Lovelace dashboard config for stable view-path resolution."""
    base_path = dashboard_frontend_path(url_path)
    request: dict[str, Any] = {"type": "lovelace/config", "force": True}
    if base_path != "lovelace":
        request["url_path"] = base_path

    try:
        response = await client.send_websocket_message(request)
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise_tool_error(
            create_error_response(
                ErrorCode.CONNECTION_FAILED,
                f"Could not load dashboard '{url_path}' for view-path resolution.",
                details=str(exc),
                context={"url_path": url_path},
                suggestions=[
                    "Check the Home Assistant connection",
                    "Retry with legacy dashboard_path if you already know the frontend route",
                ],
            )
        )
    if isinstance(response, dict) and not response.get("success", True):
        error = response.get("error", {})
        if isinstance(error, dict):
            error_code = str(error.get("code") or "").lower()
            message = str(error.get("message", error))
        else:
            error_code = ""
            message = str(error)
        missing_dashboard = (
            error_code == "config_not_found"
            or message.startswith(
                (_UNKNOWN_CONFIG_PREFIX, f"Command failed: {_UNKNOWN_CONFIG_PREFIX}")
            )
            or message in _NO_CONFIG_MESSAGES
        )
        if missing_dashboard:
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"Could not load dashboard '{url_path}': {message}",
                    context={"url_path": url_path},
                    suggestions=[
                        "Use ha_config_get_dashboard(list_only=True) to list dashboard URL paths"
                    ],
                )
            )
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to load dashboard '{url_path}': {message}",
                context={
                    "url_path": url_path,
                    "home_assistant_error_code": error_code or None,
                },
                suggestions=[
                    "Check Home Assistant permissions and logs",
                    "Retry after confirming the Home Assistant connection is healthy",
                ],
            )
        )

    config = response.get("result") if isinstance(response, dict) else response
    if not isinstance(config, dict):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Dashboard '{url_path}' returned an invalid config payload.",
                context={
                    "url_path": url_path,
                    "payload_type": type(config).__name__,
                },
            )
        )
    return cast(dict[str, Any], config)


async def resolve_dashboard_render_target(
    client: Any,
    *,
    dashboard_path: str | None,
    url_path: str | None,
    view_path: str | None,
) -> DashboardRenderTarget:
    """Adapt the existing remote config reader to canonical render addressing."""

    async def read_config(name: str) -> dict[str, Any]:
        try:
            return await fetch_dashboard_render_config(client, name)
        except ToolError as error:
            try:
                payload = json.loads(str(error))
            except (ValueError, TypeError):
                raise error from None
            if not isinstance(payload, dict) or not isinstance(
                payload.get("error"), dict
            ):
                raise error
            raise_failure(payload)

    try:
        return await _screenshots.resolve_dashboard_render_target(
            read_config,
            dashboard_path=dashboard_path,
            url_path=url_path,
            view_path=view_path,
        )
    except ToolFailure as error:
        raise_tool_error(error.data)
