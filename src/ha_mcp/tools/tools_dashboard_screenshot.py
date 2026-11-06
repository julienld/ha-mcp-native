"""Dashboard screenshot tool (opt-in, beta).

Gated behind ``enable_dashboard_screenshot``. Renders one or more Lovelace
dashboard images via the separate screenshot engine app (add-on) or a
docker-compose sidecar, and returns native MCP image blocks for visual
verification.

The companion ``include_screenshot`` / ``return_screenshot`` parameters on
``ha_config_get_dashboard`` / ``ha_config_set_dashboard`` share the same
capture path (see ``ha_mcp.dashboard_screenshot.capture``).
"""

from __future__ import annotations

import logging
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import ToolResult, tool
from ha_mcp.backends.legacy import legacy_errors
from ha_mcp.runtime.screenshot_tools import ScreenshotFacade
from ha_mcp.runtime.screenshot_values import screenshot_result

from ..dashboard_screenshot.capture import (
    capture_dashboard_images,
    validate_capture_parameters,
)
from ..dashboard_screenshot.content import (
    dashboard_image_content,
)
from ..dashboard_screenshot.paths import (
    DashboardRenderTarget,
    resolve_dashboard_render_target,
)
from ..errors import ErrorCode, create_error_response
from .helpers import log_tool_usage, raise_tool_error, register_tool_methods
from .tool_hints import read_only_hints

logger = logging.getLogger(__name__)


def _package_screenshot_result(
    *,
    captures: list[Any],
    target: DashboardRenderTarget,
    capture_failures: list[dict[str, Any]],
    capture_warnings: list[str],
) -> ToolResult:
    """Build the standalone native-image result with structured failures."""
    try:
        structured_content = screenshot_result(
            captures, target, capture_failures, capture_warnings
        )
        return ToolResult(
            content=dashboard_image_content(captures),
            structured_content=structured_content,
        )
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        error_payload = create_error_response(
            ErrorCode.IMAGE_SERIALIZATION_FAILED,
            "Rendered dashboard images could not be packaged into the MCP response.",
            details=str(exc),
            context={
                "capture_count": len(captures),
                "render_path": target.render_path,
            },
        )
        if capture_warnings:
            # The render already happened, so a theme-guard warning (e.g. a
            # failed restore) must stay visible even when packaging fails.
            error_payload["warnings"] = list(capture_warnings)
        raise_tool_error(error_payload)


class DashboardScreenshotTools:
    """Opt-in dashboard screenshot tool (gated by enable_dashboard_screenshot)."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @property
    def screenshots(self) -> DashboardScreenshotTools:
        return self

    async def screenshot(self, **arguments: Any) -> ToolResult:
        target = await resolve_dashboard_render_target(
            self._client,
            dashboard_path=arguments.pop("dashboard_path"),
            url_path=arguments.pop("url_path"),
            view_path=arguments.pop("view_path"),
        )
        validate_capture_parameters(**arguments)
        try:
            capture_failures: list[dict[str, Any]] = []
            capture_warnings: list[str] = []
            captures = await capture_dashboard_images(
                target.render_path,
                **arguments,
                partial_failures=capture_failures,
                client=self._client,
                capture_warnings=capture_warnings,
            )
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - preserve the public transport error envelope
            raise_tool_error(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    "Dashboard screenshot failed unexpectedly.",
                    details=str(exc),
                    context={"dashboard_path": target.render_path},
                )
            )
        return _package_screenshot_result(
            captures=captures,
            target=target,
            capture_failures=capture_failures,
            capture_warnings=capture_warnings,
        )

    ha_get_dashboard_screenshot = tool(
        name="ha_get_dashboard_screenshot",
        tags={"Dashboard", "beta"},
        annotations=read_only_hints("Get Dashboard Screenshot", open_world=False),
    )(log_tool_usage(legacy_errors(ScreenshotFacade.ha_get_dashboard_screenshot)))


def register_dashboard_screenshot_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register the dashboard screenshot tool when the feature flag is on.

    Set HAMCP_ENABLE_DASHBOARD_SCREENSHOT=true (beta) to enable. Mirrors the
    early-return gate used by register_filesystem_tools.
    """
    from ..config import get_global_settings

    if not get_global_settings().enable_dashboard_screenshot:
        logger.debug(
            "Dashboard screenshot tool disabled "
            "(set HAMCP_ENABLE_DASHBOARD_SCREENSHOT=true to enable)"
        )
        return

    register_tool_methods(mcp, DashboardScreenshotTools(client))
