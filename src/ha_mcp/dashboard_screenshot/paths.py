"""Stable Lovelace render-path resolution for dashboard screenshots."""

from __future__ import annotations

import json
from typing import Any, cast

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backends.legacy import legacy_errors_sync
from ha_mcp.runtime import dashboard_routes as _routes

from ..errors import ErrorCode, create_error_response
from ..tools.helpers import raise_tool_error
from .capture import _normalize_dashboard_path

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


async def _resolve_legacy_dashboard_target(
    client: Any, dashboard_path: str | None
) -> DashboardRenderTarget:
    """Validate and resolve the backward-compatible raw route form."""
    render_path = _normalize_dashboard_path(dashboard_path or "")
    parts = render_path.split("/")
    dashboard_root = parts[0]
    if len(parts) > 1 and parts[1] in _RESERVED_VIEW_PATHS:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Raw view suffix '{parts[1]}' is reserved by Home Assistant.",
                context={"url_path": dashboard_root},
            )
        )
    validated_config = await _validate_legacy_dashboard_root(client, render_path)
    if validated_config is None:
        raw_view = parts[1] if len(parts) > 1 else None
        numeric_index = _numeric_view_index(raw_view) if raw_view is not None else None
        return DashboardRenderTarget(
            url_path="lovelace",
            view_path=None,
            render_path=render_path,
            view_index=numeric_index,
            stable=False,
            warnings=(
                "Legacy built-in Lovelace routes are accepted without a stored "
                "dashboard config; the selected view cannot be verified.",
            ),
        )
    if len(parts) == 1:
        return resolve_dashboard_view(dashboard_root, validated_config, None)

    raw_view = parts[1]
    if "strategy" in validated_config:
        return DashboardRenderTarget(
            url_path=dashboard_root,
            view_path=None,
            render_path=render_path,
            view_index=None,
            stable=False,
            warnings=(
                "This strategy dashboard generates views at runtime; the raw "
                "view suffix cannot be verified against static config.",
            ),
        )
    numeric_index = _numeric_view_index(raw_view)
    if numeric_index is None:
        views = validated_config.get("views")
        view_index = (
            _raw_frontend_view_match(raw_view, views)
            if isinstance(views, list)
            else None
        )
        if view_index is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"Dashboard view path '{raw_view}' was not found.",
                    context={
                        "url_path": dashboard_root,
                        "view_path": raw_view,
                        "available_view_paths": [
                            path
                            for view in views or []
                            if (path := _configured_view_path(view)) is not None
                        ],
                    },
                    suggestions=[
                        "Use a render path returned by ha_config_get_dashboard"
                    ],
                )
            )
        assert isinstance(views, list)
        duplicate_count = sum(_configured_view_path(view) == raw_view for view in views)
        if duplicate_count > 1:
            return DashboardRenderTarget(
                url_path=dashboard_root,
                view_path=raw_view,
                render_path=_normalize_dashboard_path(f"{dashboard_root}/{raw_view}"),
                view_index=view_index,
                stable=False,
                warnings=(
                    f"Configured view path '{raw_view}' is duplicated; Home "
                    "Assistant selects the first matching view in config order.",
                ),
            )
        return resolve_dashboard_view(dashboard_root, validated_config, raw_view)

    views = validated_config.get("views")
    view_index = (
        _raw_frontend_view_match(raw_view, views) if isinstance(views, list) else None
    )
    if not isinstance(views, list) or view_index is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Dashboard view index '{raw_view}' was not found.",
                context={
                    "url_path": dashboard_root,
                    "view_index": numeric_index,
                    "available_view_count": len(views)
                    if isinstance(views, list)
                    else 0,
                },
                suggestions=["Use a render path returned by ha_config_get_dashboard"],
            )
        )
    # Canonicalize to the verified numeric route so a stray trailing segment
    # (e.g. "wall-panel/1/debug") cannot reach the engine unverified — only
    # parts[1] was validated against the dashboard config.
    render_path = _normalize_dashboard_path(f"{dashboard_root}/{raw_view}")
    warnings = await _numeric_view_warning(
        client,
        render_path,
        config=validated_config,
        matched_view_index=view_index,
    )
    if not warnings:
        warnings.append(
            f"Numeric view index '{render_path}' is fragile; assign the view a "
            "unique views[].path and use url_path/view_path addressing."
        )
    return DashboardRenderTarget(
        url_path=dashboard_root,
        view_path=None,
        render_path=render_path,
        view_index=view_index,
        stable=False,
        warnings=tuple(warnings),
    )


async def resolve_dashboard_render_target(
    client: Any,
    *,
    dashboard_path: str | None,
    url_path: str | None,
    view_path: str | None,
) -> DashboardRenderTarget:
    """Resolve legacy raw or structured screenshot addressing."""
    if dashboard_path is not None and url_path is not None:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Use dashboard_path or url_path/view_path, not both.",
                context={
                    "dashboard_path": dashboard_path,
                    "url_path": url_path,
                    "view_path": view_path,
                },
            )
        )
    if url_path is None:
        if view_path is not None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "url_path is required when view_path is supplied.",
                    context={"view_path": view_path},
                )
            )
        return await _resolve_legacy_dashboard_target(client, dashboard_path)

    if not url_path.strip():
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "url_path cannot be empty.",
                context={"url_path": url_path},
            )
        )
    if dashboard_frontend_path(url_path) == "lovelace" and view_path is None:
        return DashboardRenderTarget(
            url_path="lovelace",
            view_path=None,
            render_path="lovelace",
            view_index=None,
            stable=False,
            warnings=(
                "No named view path was supplied; the built-in dashboard base route "
                "renders the first view visible to Puppet's Home Assistant user.",
            ),
        )
    config = await fetch_dashboard_render_config(client, url_path)
    return resolve_dashboard_view(url_path, config, view_path)


async def _validate_legacy_dashboard_root(
    client: Any, render_path: str
) -> dict[str, Any] | None:
    """Prove a custom raw-route root resolves as a Lovelace dashboard.

    Fetching ``lovelace/config`` works for storage- and YAML-mode dashboards,
    unlike the storage registry list. The returned config is reused when a
    numeric view needs a stable-alias warning.
    """
    dashboard_root = render_path.split("/", maxsplit=1)[0]
    if dashboard_root == "lovelace":
        try:
            return await fetch_dashboard_render_config(client, dashboard_root)
        except ToolError as exc:
            try:
                error_payload = json.loads(str(exc))
            except (json.JSONDecodeError, TypeError):
                # A non-JSON ToolError (e.g. a WS-layer error whose str() is not
                # the raise_tool_error JSON form) must surface as its original
                # ToolError, not as the leaked parse exception.
                raise exc from None
            if (
                isinstance(error_payload, dict)
                and isinstance(error_payload.get("error"), dict)
                and error_payload["error"].get("code") == "RESOURCE_NOT_FOUND"
            ):
                return None
            raise
    return await fetch_dashboard_render_config(client, dashboard_root)


async def _numeric_view_warning(
    client: Any,
    render_path: str,
    *,
    config: dict[str, Any] | None = None,
    matched_view_index: int | None = None,
) -> list[str]:
    """Suggest a stable named route when a legacy numeric index has one."""
    parts = render_path.split("/", maxsplit=1)
    if len(parts) < 2:
        return []
    url_path, raw_view = parts
    index = _numeric_view_index(raw_view)
    if index is None:
        return []
    if config is None:
        config = await fetch_dashboard_render_config(client, url_path)
    views = config.get("views")
    if not isinstance(views, list):
        return []
    view_index = index if matched_view_index is None else matched_view_index
    if view_index >= len(views):
        return []
    view = views[view_index]
    stable_path = view.get("path") if isinstance(view, dict) else None
    if not isinstance(stable_path, str) or not stable_path.strip():
        return []
    if _view_path_requires_numeric_fallback(stable_path, view_index, views):
        return []
    matching_paths = [
        candidate_path
        for candidate in views
        if (candidate_path := _configured_view_path(candidate)) is not None
    ]
    if matching_paths.count(stable_path) != 1:
        return []
    canonical = _safe_named_render_path(dashboard_frontend_path(url_path), stable_path)
    if canonical is None:
        return []
    return [
        f"Numeric view index '{render_path}' is fragile; use the stable render "
        + f"path '{canonical}' or url_path/view_path addressing."
    ]
