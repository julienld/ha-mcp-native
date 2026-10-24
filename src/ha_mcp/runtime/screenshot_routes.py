"""Lovelace screenshot addressing shared by Core and remote config readers."""

from collections.abc import Awaitable, Callable
from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tool_api import ToolFailure as ToolError

from .dashboard_routes import (
    _RESERVED_VIEW_PATHS,
    DashboardRenderTarget,
    _configured_view_path,
    _normalize_dashboard_path,
    _numeric_view_index,
    _raw_frontend_view_match,
    _safe_named_render_path,
    _view_path_requires_numeric_fallback,
    dashboard_frontend_path,
    resolve_dashboard_view,
)
from .errors import raise_failure as raise_tool_error

DashboardConfigReader = Callable[[str], Awaitable[dict[str, Any]]]


async def _resolve_legacy_dashboard_target(
    read_config: DashboardConfigReader, dashboard_path: str | None
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
    validated_config = await _validate_legacy_dashboard_root(read_config, render_path)
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
        read_config,
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
    read_config: DashboardConfigReader,
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
        return await _resolve_legacy_dashboard_target(read_config, dashboard_path)

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
    config = await read_config(url_path)
    return resolve_dashboard_view(url_path, config, view_path)


async def _validate_legacy_dashboard_root(
    read_config: DashboardConfigReader, render_path: str
) -> dict[str, Any] | None:
    """Prove a custom raw-route root resolves as a Lovelace dashboard.

    Fetching ``lovelace/config`` works for storage- and YAML-mode dashboards,
    unlike the storage registry list. The returned config is reused when a
    numeric view needs a stable-alias warning.
    """
    dashboard_root = render_path.split("/", maxsplit=1)[0]
    if dashboard_root == "lovelace":
        try:
            return await read_config(dashboard_root)
        except ToolError as exc:
            if exc.code == "RESOURCE_NOT_FOUND":
                return None
            raise
    return await read_config(dashboard_root)


async def _numeric_view_warning(
    read_config: DashboardConfigReader,
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
        config = await read_config(url_path)
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
