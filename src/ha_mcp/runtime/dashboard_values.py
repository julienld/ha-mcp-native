"""Dashboard values, transformations and public result projections."""

from __future__ import annotations

import copy
import json
from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.utils.config_hash import compute_config_hash
from ha_mcp.utils.python_sandbox import (
    PythonSandboxError,
    PythonSandboxExecutionError,
    format_sandbox_error,
    safe_execute,
)

from .dashboard_errors import raise_dashboard_edit_error
from .dashboard_routes import dashboard_render_paths, match_dashboard_view
from .dashboard_validation import reject_malformed_dashboard_lists
from .errors import raise_failure as raise_tool_error


def _large_dashboard_replacement_warning(size: int) -> str | None:
    """Keep the full-replacement guidance identical on both backends."""
    if size < _LARGE_DASHBOARD_CONFIG_SIZE:
        return None
    return (
        f"Replaced large config ({size:,} bytes). "
        "Consider patch for known paths or python_transform for pattern-based edits."
    )


def _attach_dashboard_render_paths(
    result: dict[str, Any],
    url_path: str | None,
    config: dict[str, Any] | None,
) -> None:
    """Attach canonical per-view render routes without mutating config."""
    render_paths, warnings = dashboard_render_paths(url_path, config)
    result["render_paths"] = render_paths
    if warnings:
        result.setdefault("warnings", []).extend(warnings)


_LARGE_DASHBOARD_CONFIG_SIZE = 10000


def scoped_view_get_result(
    url_path: str | None,
    config: Any,
    *,
    view_path: str,
    config_hash: str | None,
    config_size: int,
) -> dict[str, Any]:
    """Build the view-scoped get-mode envelope (issue #2010).

    Raises the shared matcher's structured errors for an empty, unknown,
    or ambiguous ``view_path`` and for strategy dashboards (no static
    views). Pure config walking — works whether or not the dashboard
    screenshot beta feature is enabled.
    """
    if not isinstance(config, dict):
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                "Dashboard config is unavailable, so view_path cannot be resolved.",
                context={"url_path": url_path, "view_path": view_path},
                suggestions=["Retry without view_path to inspect the raw response"],
            )
        )
    view_index, view = match_dashboard_view(
        url_path or "default",
        config,
        view_path,
        strategy_suggestions=(
            "Omit view_path — strategy dashboards generate their views at "
            "runtime, so there is no static view config to return",
        ),
    )
    views = config.get("views")
    view_count = len(views) if isinstance(views, list) else 0
    get_result: dict[str, Any] = {
        "success": True,
        "action": "get",
        "url_path": url_path,
        "view_path": view_path,
        "view_index": view_index,
        "view_count": view_count,
        "view": view,
        "config_hash": config_hash,
        "config_size_bytes": config_size,
        "view_size_bytes": len(json.dumps(view)),
        "hint": (
            f"Scoped to view '{view_path}' — config['views'][{view_index}] "
            f"of {view_count} views. config_hash and config_size_bytes "
            "cover the FULL dashboard config, so "
            "ha_config_set_dashboard(python_transform=...) edits addressed "
            f"via config['views'][{view_index}] validate as-is. Do NOT "
            "pass this view object as the config parameter — that would "
            "replace the entire dashboard. Omit view_path for the full "
            "config."
        ),
    }
    _attach_dashboard_render_paths(get_result, url_path, config)
    render_paths = get_result.get("render_paths")
    if isinstance(render_paths, list):
        get_result["render_paths"] = [
            row
            for row in render_paths
            if isinstance(row, dict) and row.get("view_index") == view_index
        ]
    return get_result


def validate_strategy_dashboard_replacement(
    url_path: str,
    *,
    was_strategy_dashboard: bool,
    replacement_config: dict[str, Any],
    action: str,
) -> None:
    """Prevent this tool from taking control of a strategy dashboard."""
    if not was_strategy_dashboard or "strategy" in replacement_config:
        return
    raise_dashboard_edit_error(
        url_path,
        "strategy_conversion",
        "Strategy dashboards cannot be converted to custom dashboards via this tool",
        False,
        action,
    )


def apply_dashboard_python_transform(
    url_path: str, python_transform: str, current_config: dict[str, Any]
) -> dict[str, Any]:
    """Run ``python_transform`` against ``current_config`` in the sandbox."""
    was_strategy_dashboard = "strategy" in current_config
    try:
        transformed_config = safe_execute(
            python_transform, copy.deepcopy(current_config)
        )
    except PythonSandboxError as e:
        message, suggestions = format_sandbox_error(e, python_transform)
        # A path-shape mismatch (IndexError/KeyError) is almost always
        # a hallucinated path; steer the retry toward search mode so
        # the next transform is built from a verified python_path.
        if isinstance(e, PythonSandboxExecutionError) and isinstance(
            e.__cause__, (IndexError, KeyError)
        ):
            suggestions = [
                "Call ha_config_get_dashboard with card_type=..., "
                "entity_id=..., or heading=... to get the verified "
                "python_path for the target card, then build "
                "python_transform from that path",
                *suggestions,
            ]
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_FAILED,
                message,
                suggestions=suggestions,
                context={"action": "python_transform", "url_path": url_path},
            )
        )
    validate_strategy_dashboard_replacement(
        url_path,
        was_strategy_dashboard=was_strategy_dashboard,
        replacement_config=transformed_config,
        action="python_transform",
    )
    reject_malformed_dashboard_lists(
        transformed_config, url_path, source="python_transform"
    )
    return transformed_config


def get_dashboard_result(
    url_path: str | None,
    config: Any,
    *,
    original_url_path: str | None,
    view_path: str | None,
) -> dict[str, Any]:
    config_hash = compute_config_hash(config) if isinstance(config, dict) else None

    # Calculate config size for progressive disclosure hint
    config_size = len(json.dumps(config)) if isinstance(config, dict) else 0

    if view_path is not None:
        get_result = scoped_view_get_result(
            url_path,
            config,
            view_path=view_path,
            config_hash=config_hash,
            config_size=config_size,
        )
    else:
        get_result = {
            "success": True,
            "action": "get",
            "url_path": url_path,
            "config": config,
            "config_hash": config_hash,
            "config_size_bytes": config_size,
        }
        _attach_dashboard_render_paths(
            get_result,
            url_path,
            config if isinstance(config, dict) else None,
        )
        # Add hint for large configs (progressive disclosure) - 10KB ≈ 2-3k tokens
        if config_size >= 10000:
            get_result["hint"] = (
                f"Large config ({config_size:,} bytes). Pass view_path=... "
                "to fetch a single view, or use "
                "ha_config_get_dashboard(entity_id=...) to find card positions, "
                "then ha_config_set_dashboard(python_transform=...) "
                "instead of full config replacement."
            )
    # Surface the original caller-passed identifier when the lazy
    # resolver canonicalised it (parity with delete_dashboard's
    # resolved_id field). Caller can use this to detect that their
    # input was an internal id rather than a url_path.
    if original_url_path is not None and original_url_path != url_path:
        get_result["resolved_from"] = original_url_path

    return get_result


def edit_dashboard_result(
    url_path: str,
    edit: dict[str, Any],
    action: str,
    pre_resolved_from: str | None,
    *,
    python_transform: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "success": True,
        "action": action,
        "url_path": url_path,
        "config_hash": edit["config_hash"],
        "write_committed": edit["write_committed"],
        "post_write_verified": edit["post_write_verified"],
        "message": f"Dashboard {url_path} updated via "
        + ("Python transform" if action == "python_transform" else "patch"),
    }
    if python_transform is not None:
        result["python_expression"] = python_transform
    if pre_resolved_from is not None:
        result["resolved_from"] = pre_resolved_from
    if edit.get("warnings"):
        result["warnings"] = list(edit["warnings"])
    if edit.get("unchanged"):
        result["unchanged"] = True
        result["message"] = f"Dashboard {url_path} unchanged"
    if edit["post_write_verified"]:
        _attach_dashboard_render_paths(result, url_path, edit["config"])
    return result


def set_dashboard_result(
    url_path: str,
    *,
    dashboard_exists: bool,
    dashboard_id: str | None,
    config_updated: bool,
    metadata_updated: bool,
    warnings: list[str],
    pre_resolved_from: str | None,
) -> dict[str, Any]:
    result_dict: dict[str, Any] = {
        "success": True,
        "action": "create" if not dashboard_exists else "update",
        "url_path": url_path,
        "dashboard_id": dashboard_id,
        "dashboard_created": not dashboard_exists,
        "config_updated": config_updated,
        "metadata_updated": metadata_updated,
        "message": f"Dashboard {url_path} {'created' if not dashboard_exists else 'updated'} successfully",
    }

    if warnings:
        result_dict["warnings"] = warnings
    if pre_resolved_from is not None:
        # This marks identifier canonicalization only. Exact url_path
        # matches deliberately omit it; callers use ``action`` to tell
        # whether the create-or-update operation updated an existing target.
        result_dict["resolved_from"] = pre_resolved_from

    return result_dict
