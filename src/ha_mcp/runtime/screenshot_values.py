"""Public screenshot metadata, shared by native and remote image adapters."""

from typing import Any

from ha_mcp.dashboard_screenshot.content import (
    dashboard_screenshot_metadata,
    dashboard_screenshot_warnings,
)

from .dashboard_routes import DashboardRenderTarget


def screenshot_result(
    captures: list[Any],
    target: DashboardRenderTarget,
    failures: list[dict[str, Any]],
    capture_warnings: list[str],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "success": True,
        "url_path": target.url_path,
        "view_path": target.view_path,
        "view_index": target.view_index,
        "render_path": target.render_path,
        "stable_addressing": target.stable,
        "screenshot_count": len(captures),
        "screenshots": dashboard_screenshot_metadata(captures, target.render_path),
    }
    if failures:
        result["partial"] = True
        result["screenshot_failures"] = failures
    warnings = [
        *target.warnings,
        *dashboard_screenshot_warnings(captures),
        *capture_warnings,
    ]
    if warnings:
        result["warnings"] = warnings
    return result
