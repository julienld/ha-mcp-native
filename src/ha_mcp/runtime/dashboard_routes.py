"""Pure Lovelace view routing shared by native reads and screenshot adapters."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tool_api import ToolFailure as ToolError

from .errors import raise_failure as raise_tool_error

_FORBIDDEN_PATH_BITS = ("://", "//", "..", "@", "\\", "?", "#")


def _normalize_dashboard_path(dashboard_path: str) -> str:
    """Return a safe, stripped, unencoded dashboard path or raise ToolError.

    Rejects URL syntax, traversal, and control characters. External raw-path
    callers must additionally verify the first segment against Home
    Assistant's registered Lovelace dashboards; structured and config-tool
    callers derive it from dashboard configuration instead.
    """
    raw = (dashboard_path or "").strip()
    if not raw:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_MISSING_PARAMETER,
                "dashboard_path is required (e.g. 'lovelace/0' or 'my-dashboard').",
                suggestions=["Pass a Lovelace path such as 'lovelace/0'"],
            )
        )
    if any(bit in raw for bit in _FORBIDDEN_PATH_BITS) or any(
        ord(c) < 0x20 for c in raw
    ):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Invalid dashboard_path {dashboard_path!r}: pass a plain "
                "Lovelace path like 'lovelace/0' or 'my-dashboard/kitchen'.",
                details="Path may not contain URLs, query strings, fragments, "
                "'..' segments, backslashes, or control characters.",
                context={"dashboard_path": dashboard_path},
            )
        )
    segments = [seg for seg in raw.strip("/").split("/") if seg not in ("", ".")]
    if not segments:
        # e.g. "/" or "/." — the engine would serve its config/UI HTML for the
        # root path, not a dashboard PNG, which would be a confusing silent
        # failure. Require a concrete view path.
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Invalid dashboard_path {dashboard_path!r}: cannot be empty or "
                "the root path.",
                details="Pass a specific Lovelace view, e.g. 'lovelace/0'.",
                context={"dashboard_path": dashboard_path},
            )
        )
    return "/".join(segments)


_JS_DECIMAL_NUMBER = re.compile(
    r"[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[eE][+-]?\d+)?\Z"
)


_JS_RADIX_NUMBER = re.compile(r"0(?P<base>[xXbBoO])(?P<digits>[0-9a-fA-F]+)\Z")


_RESERVED_VIEW_PATHS = frozenset({"hass-unused-entities"})


_DECODE_URI_RESERVED_VIEW_CHARS = frozenset("$&+,:;=")


@dataclass(frozen=True, slots=True)
class DashboardRenderTarget:
    """Resolved frontend route and audit metadata for a screenshot request."""

    url_path: str
    view_path: str | None
    render_path: str
    view_index: int | None
    stable: bool
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.stable and not self.warnings:
            raise ValueError(
                "a non-stable DashboardRenderTarget must carry at least one warning"
            )


def dashboard_frontend_path(url_path: str | None) -> str:
    """Map a dashboard URL path to its Lovelace frontend base route."""
    if not url_path or url_path in {"default", "lovelace"}:
        return "lovelace"
    return _normalize_dashboard_path(url_path)


def _configured_view_path(view: Any) -> str | None:
    """Return a non-blank configured view path, if one exists."""
    if not isinstance(view, dict):
        return None
    path = view.get("path")
    if not isinstance(path, str) or not path.strip():
        return None
    return path


def _numeric_view_index(path: str) -> int | None:
    """Mirror HA's ``Number(selectedView)`` integer-index route collision."""
    numeric_text = path.strip()
    radix_match = _JS_RADIX_NUMBER.fullmatch(numeric_text)
    if radix_match is not None:
        base = {"x": 16, "b": 2, "o": 8}[radix_match.group("base").lower()]
        try:
            return int(radix_match.group("digits"), base)
        except ValueError:
            return None
    if _JS_DECIMAL_NUMBER.fullmatch(numeric_text) is None:
        return None
    try:
        numeric = float(numeric_text)
    except ValueError:
        return None
    if not math.isfinite(numeric) or numeric < 0 or not numeric.is_integer():
        return None
    return int(numeric)


def _view_path_requires_numeric_fallback(
    path: str, index: int, views: list[Any]
) -> bool:
    """Return whether HA's frontend route cannot select this exact view by path."""
    return (
        _numeric_view_index(path) is not None
        or path != path.strip()
        or "/" in path
        or any(char in path for char in _DECODE_URI_RESERVED_VIEW_CHARS)
        or path in _RESERVED_VIEW_PATHS
        or _raw_frontend_view_match(path, views) != index
    )


def _numeric_fallback_suffix(index: int, views: list[Any]) -> str:
    """Choose a Number()-equivalent index route that HA resolves to ``index``."""
    decimal_places = 0
    candidate = str(index)
    while _raw_frontend_view_match(candidate, views) != index:
        decimal_places += 1
        candidate = f"{index}.{'0' * decimal_places}"
    return candidate


def _raw_frontend_view_match(raw_view: str, views: list[Any]) -> int | None:
    """Mirror HA's ordered path-or-Number view selection loop."""
    numeric_index = _numeric_view_index(raw_view)
    for index, view in enumerate(views):
        if _configured_view_path(view) == raw_view or index == numeric_index:
            return index
    return None


def _render_path_metadata(
    base_path: str,
    index: int,
    view: Any,
    path_counts: Counter[str],
    views: list[Any],
) -> tuple[dict[str, Any], bool, bool]:
    """Resolve one view to stable or safe numeric render metadata."""
    view_config = view if isinstance(view, dict) else {}
    stable_path = _configured_view_path(view)
    ambiguous_path = stable_path is not None and path_counts[stable_path] > 1
    unusable_path = stable_path is not None and _view_path_requires_numeric_fallback(
        stable_path, index, views
    )
    fallback_suffix = _numeric_fallback_suffix(index, views)
    suffix = (
        stable_path
        if stable_path is not None and not ambiguous_path and not unusable_path
        else fallback_suffix
    )
    invalid_path = unusable_path
    try:
        render_path = _normalize_dashboard_path(f"{base_path}/{suffix}")
        if suffix == stable_path and render_path != f"{base_path}/{stable_path}":
            invalid_path = True
            render_path = _normalize_dashboard_path(f"{base_path}/{fallback_suffix}")
    except ToolError:
        # The configured path is unusable, but its numeric index remains a safe
        # route to the same view.
        invalid_path = True
        render_path = _normalize_dashboard_path(f"{base_path}/{fallback_suffix}")

    metadata: dict[str, Any] = {
        "url_path": base_path,
        "view_index": index,
        "view_path": stable_path,
        "title": view_config.get("title"),
        "render_path": render_path,
        "stable": stable_path is not None and not ambiguous_path and not invalid_path,
    }
    if ambiguous_path:
        metadata["ambiguous_path"] = True
    if invalid_path:
        metadata["invalid_path"] = True
    return (
        metadata,
        stable_path is None or ambiguous_path or invalid_path,
        ambiguous_path,
    )


def dashboard_render_paths(
    url_path: str | None, config: dict[str, Any] | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build canonical render metadata without mutating dashboard config."""
    base_path = dashboard_frontend_path(url_path)
    if not isinstance(config, dict):
        return [], []
    if "strategy" in config:
        return (
            [
                {
                    "url_path": base_path,
                    "view_index": None,
                    "view_path": None,
                    "title": None,
                    "render_path": base_path,
                    "stable": False,
                    "strategy_dashboard": True,
                }
            ],
            [
                "Strategy dashboard views are generated at runtime; only the "
                + "dashboard base render path is available."
            ],
        )

    views = config.get("views")
    if not isinstance(views, list):
        return [], []

    configured_paths = [
        path for view in views if (path := _configured_view_path(view)) is not None
    ]
    path_counts = Counter(configured_paths)
    paths: list[dict[str, Any]] = []
    fallback_count = 0
    ambiguous_count = 0
    for index, view in enumerate(views):
        metadata, used_fallback, ambiguous_path = _render_path_metadata(
            base_path, index, view, path_counts, views
        )
        if used_fallback:
            fallback_count += 1
        if ambiguous_path:
            ambiguous_count += 1
        paths.append(metadata)

    warnings: list[str] = []
    if fallback_count:
        warnings.append(
            f"{fallback_count} dashboard view(s) have no usable stable path; "
            "their render paths use fragile numeric indexes."
        )
    if ambiguous_count:
        warnings.append(
            f"{ambiguous_count} dashboard view(s) share a duplicate configured path; "
            "numeric render paths are returned for those ambiguous views."
        )
    return paths, warnings


def _safe_named_render_path(base_path: str, view_path: str) -> str | None:
    """Return an unchanged safe named route, or ``None`` when it is unusable."""
    expected = f"{base_path}/{view_path}"
    try:
        normalized = _normalize_dashboard_path(expected)
    except ToolError:
        return None
    return normalized if normalized == expected else None


def _fallback_view_target(
    *,
    base_path: str,
    view_path: str,
    view_index: int,
    views: list[Any],
    reason: str,
) -> DashboardRenderTarget:
    """Build one verified numeric fallback target for an unusable named path."""
    fallback_suffix = _numeric_fallback_suffix(view_index, views)
    return DashboardRenderTarget(
        url_path=base_path,
        view_path=view_path,
        render_path=_normalize_dashboard_path(f"{base_path}/{fallback_suffix}"),
        view_index=view_index,
        stable=False,
        warnings=(
            f"Configured view path '{view_path}' {reason}; the verified numeric "
            f"fallback '{base_path}/{fallback_suffix}' is used.",
        ),
    )


def match_dashboard_view(
    url_path: str,
    config: dict[str, Any],
    view_path: str,
    *,
    strategy_suggestions: tuple[str, ...] = (
        "Use dashboard_path with the frontend route instead",
    ),
) -> tuple[int, dict[str, Any]]:
    """Match one named Lovelace view by its configured ``views[].path``.

    Shared by screenshot view resolution and get-mode view scoping so both
    honour identical semantics: exact match on the configured path, with
    structured errors for an empty path, a strategy dashboard (no static
    views to match), and a missing/ambiguous path (the not-found error lists
    the available paths). Returns ``(view_index, view)``.

    ``strategy_suggestions`` lets each caller phrase the strategy-dashboard
    remediation in its own vocabulary (the screenshot tools have a
    ``dashboard_path`` route parameter; config scoping does not).
    """
    if not view_path.strip():
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "view_path cannot be empty.",
                context={"view_path": view_path},
            )
        )
    if "strategy" in config:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Strategy dashboards do not expose static named view paths.",
                context={
                    "url_path": url_path,
                    "view_path": view_path,
                },
                suggestions=list(strategy_suggestions),
            )
        )
    views = config.get("views")
    if not isinstance(views, list):
        views = []
    matches = [
        (index, view)
        for index, view in enumerate(views)
        if _configured_view_path(view) == view_path
    ]
    if len(matches) != 1:
        available = [
            view.get("path")
            for view in views
            if isinstance(view, dict) and isinstance(view.get("path"), str)
        ]
        reason = "not found" if not matches else "ambiguous"
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Dashboard view path '{view_path}' is {reason}.",
                context={
                    "url_path": url_path,
                    "view_path": view_path,
                    "available_view_paths": available,
                },
                suggestions=["Use a view_path returned by ha_config_get_dashboard"],
            )
        )
    return matches[0]


def resolve_dashboard_view(
    url_path: str,
    config: dict[str, Any],
    view_path: str | None,
) -> DashboardRenderTarget:
    """Resolve one named Lovelace view to a canonical frontend route."""
    base_path = dashboard_frontend_path(url_path)
    if view_path is None:
        views = config.get("views")
        has_static_views = isinstance(views, list) and bool(views)
        if "strategy" in config:
            warning = (
                "No view_path was supplied; this strategy dashboard generates "
                "its views at runtime, so only the base route is available."
            )
        elif has_static_views:
            warning = (
                "No view_path was supplied; the dashboard base route renders "
                "the first view visible to Puppet's Home Assistant user."
            )
        else:
            warning = (
                "No view_path was supplied and the dashboard has no static views; "
                "only the base route is available."
            )
        return DashboardRenderTarget(
            url_path=base_path,
            view_path=None,
            render_path=base_path,
            view_index=None,
            stable=False,
            warnings=(warning,),
        )

    view_index, _ = match_dashboard_view(url_path, config, view_path)
    cleaned_view_path = view_path
    views = config.get("views")
    if not isinstance(views, list):
        views = []
    render_path = _safe_named_render_path(base_path, cleaned_view_path)
    if render_path is None:
        return _fallback_view_target(
            base_path=base_path,
            view_path=cleaned_view_path,
            view_index=view_index,
            views=views,
            reason="is unsafe for screenshot transport",
        )
    if _view_path_requires_numeric_fallback(cleaned_view_path, view_index, views):
        return _fallback_view_target(
            base_path=base_path,
            view_path=cleaned_view_path,
            view_index=view_index,
            views=views,
            reason=(
                "cannot be selected reliably as a named Home Assistant frontend route"
            ),
        )
    return DashboardRenderTarget(
        url_path=base_path,
        view_path=cleaned_view_path,
        render_path=render_path,
        view_index=view_index,
        stable=True,
    )
