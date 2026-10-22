"""Canonical dashboard screenshot arguments, independent of the HA connection."""

from typing import Annotated, Any, Literal, Protocol

from pydantic import Field

from ha_mcp.dashboard_screenshot.capture import (
    DEFAULT_HEIGHT,
    DEFAULT_RENDER_TIMEOUT_SECONDS,
    DEFAULT_WAIT_MS,
    DEFAULT_WIDTH,
    FULL_PAGE_PARAM_DESC,
    Orientation,
    ScreenshotFormat,
    ViewportPreset,
)
from ha_mcp.tools.coercion import JSON_STRING_COERCION


class ScreenshotService(Protocol):
    async def screenshot(self, **arguments: Any) -> Any: ...


class ScreenshotFacade:
    def __init__(self, screenshots: ScreenshotService) -> None:
        self.screenshots = screenshots

    async def ha_get_dashboard_screenshot(
        self,
        dashboard_path: Annotated[
            str | None,
            Field(
                description="Legacy Lovelace frontend path to render, e.g. "
                "'lovelace/0' (default dashboard, first view), "
                "'lovelace-home/kitchen', or 'my-dashboard'. Leading slash "
                "optional. Prefer url_path + view_path for a stable "
                "named view. Mutually exclusive with url_path."
            ),
        ] = None,
        url_path: Annotated[
            str | None,
            Field(
                description="Stable dashboard URL path, e.g. 'lovelace-home' "
                "or 'default'. Use with view_path instead of dashboard_path."
            ),
        ] = None,
        view_path: Annotated[
            str | None,
            Field(
                description="Stable Lovelace views[].path value to render. "
                "Requires url_path."
            ),
        ] = None,
        width: Annotated[
            int, Field(description="Viewport width in px.", ge=64, le=4096)
        ] = DEFAULT_WIDTH,
        height: Annotated[
            int | Literal["auto"],
            Field(
                description="Viewport height in px (64-4096), or 'auto' for "
                "content height. full_page=True is a compatibility alias for "
                "'auto'."
            ),
        ] = DEFAULT_HEIGHT,
        viewport_presets: Annotated[
            list[ViewportPreset] | None,
            Field(
                description="Render one or more named responsive viewports in "
                "this order: mobile (390x844), tablet (768x1024), desktop "
                "(1280x800). Overrides width/height.",
                min_length=1,
                max_length=3,
            ),
            JSON_STRING_COERCION,
        ] = None,
        orientation: Annotated[
            Orientation | None,
            Field(
                description="Optional responsive orientation. Swaps viewport "
                "dimensions when needed; this does not rotate final pixels."
            ),
        ] = None,
        zoom: Annotated[
            float, Field(description="Page zoom factor (1.0 = 100%).", ge=0.1, le=5.0)
        ] = 1.0,
        wait_ms: Annotated[
            int,
            Field(
                description="Extra render-settle time (ms) after the dashboard "
                "reports loaded. Raise it if a chart card (ApexCharts, "
                "mini-graph, history-graph) comes back blank.",
                ge=0,
                le=30000,
            ),
        ] = DEFAULT_WAIT_MS,
        full_page: Annotated[
            bool,
            Field(
                description=f"{FULL_PAGE_PARAM_DESC[:1].upper()}"
                f"{FULL_PAGE_PARAM_DESC[1:]}. Uses Puppet's native auto-height "
                "capture (currently capped at 4000 px); raise wait_ms for "
                "lazy cards."
            ),
        ] = False,
        theme: Annotated[
            str | None,
            Field(
                description="Installed Home Assistant frontend theme name, applied to this render."
            ),
        ] = None,
        dark_mode: Annotated[
            bool,
            Field(description="Render the requested theme in dark mode."),
        ] = False,
        language: Annotated[
            str | None,
            Field(description="Frontend language code, e.g. 'en' or 'de'."),
        ] = None,
        image_format: Annotated[
            ScreenshotFormat,
            Field(description="Image format: png, jpeg, webp, or bmp."),
        ] = "png",
        render_timeout_seconds: Annotated[
            float,
            Field(
                description="HTTP render timeout in seconds.",
                ge=1,
                le=300,
            ),
        ] = DEFAULT_RENDER_TIMEOUT_SECONDS,
    ) -> Any:
        """Get rendered images of a Home Assistant Lovelace dashboard view.

        When not to use: while reading or writing dashboard configuration, use
        ha_config_get_dashboard(include_screenshot=True) or
        ha_config_set_dashboard(return_screenshot=True) for a single workflow.

        Use it for repeatable visual checks, including ordered mobile, tablet,
        and desktop captures. Puppet reports image bytes but does not confirm
        that the frontend accepted a requested theme or language; structured
        metadata therefore records the values sent to the engine. A requested
        theme or dark_mode persists on the engine account's profile; the change
        is reported in warnings but not undone (see ha_manage_theme
        action='set_engine_theme').

        To change the Puppet engine app (add-on) itself (keep_browser_open,
        restart), use ha_manage_app.
        """
        return await self.screenshots.screenshot(
            dashboard_path=dashboard_path,
            url_path=url_path,
            view_path=view_path,
            width=width,
            height=height,
            viewport_presets=viewport_presets,
            orientation=orientation,
            zoom=zoom,
            wait_ms=wait_ms,
            full_page=full_page,
            theme=theme,
            dark_mode=dark_mode,
            language=language,
            image_format=image_format,
            render_timeout_seconds=render_timeout_seconds,
        )
