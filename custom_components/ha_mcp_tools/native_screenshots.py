"""Adapt the existing screenshot HTTP engine to HA-scoped discovery and Core themes."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.dashboard_screenshot.capture import capture_dashboard_images
from ha_mcp.dashboard_screenshot.content import (
    dashboard_screenshot_metadata,
    dashboard_screenshot_warnings,
)
from ha_mcp.dashboard_screenshot.provision import (
    ENGINE_PORT,
    ENGINE_SLUG_SUFFIXES,
    EngineTarget,
    _select_started_verified_puppet,
)
from ha_mcp.runtime.dashboard_routes import resolve_dashboard_view
from ha_mcp.tool_api import CallContext, ToolFailure, ToolImage, ToolResult
from ha_mcp.utils.owned_executor import await_owned_io

from .native_operations.registry_access import authorize_actor
from .native_operations.screenshot_theme import NativeThemeObserver
from .native_server_policy import setting_value


@dataclass(slots=True)
class NativeScreenshots:
    manager: Any
    actor: CallContext

    async def _discover(self) -> tuple[str, str | None]:
        from homeassistant.components.hassio import get_supervisor_client

        client = get_supervisor_client(self.manager.hass)
        async with asyncio.timeout(15):
            await authorize_actor(self.actor, self.manager.backend.authorize)
            apps = await client.addons.list()
            infos = []
            for app in apps:
                if app.slug.endswith(ENGINE_SLUG_SUFFIXES):
                    await authorize_actor(self.actor, self.manager.backend.authorize)
                    info = await client.addons.addon_info(app.slug)
                    infos.append((app.slug, info.to_dict()))
            _slug, selected = _select_started_verified_puppet(infos)
        hostname = selected.get("hostname") or selected.get("ip_address")
        if not hostname:
            raise ToolFailure(
                "SERVICE_CALL_FAILED",
                "The screenshot engine app has no reachable hostname",
                status="not_started",
            )
        token = (selected.get("options") or {}).get("access_token")
        return f"http://{hostname}:{ENGINE_PORT}", str(token).strip() if token else None

    async def _target(self) -> tuple[str, str | None]:
        explicit = (
            str(
                setting_value(
                    self.manager.policy.flags, "dashboard_screenshot_engine_url"
                )
                or ""
            )
            .strip()
            .rstrip("/")
        )
        if explicit:
            parsed = urlsplit(explicit)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ToolFailure(
                    "CONFIG_VALIDATION_FAILED",
                    "The configured screenshot engine needs an absolute HTTP or HTTPS URL",
                    status="not_started",
                )
        available = "hassio" in self.manager.hass.config.components
        if not available:
            if explicit:
                return explicit, None
            raise ToolFailure(
                "SERVICE_CALL_FAILED",
                "No screenshot engine is configured. Set the screenshot engine URL for this entry, or install and start the Puppet app on Home Assistant OS.",
                status="not_started",
            )
        try:
            discovered, token = await self._discover()
        except Exception:
            if not explicit:
                raise
            return explicit, None
        # A credential discovered for one app is not evidence about an
        # unrelated explicitly configured renderer.
        return (
            (explicit, token if explicit == discovered else None)
            if explicit
            else (discovered, token)
        )

    async def capture(
        self,
        result: dict[str, Any],
        url_path: str,
        config: dict[str, Any] | None,
        view_path: str | None,
        *,
        after_write: bool,
    ) -> ToolResult | dict[str, Any]:
        try:
            return await self._capture(result, url_path, config, view_path)
        except Exception as error:
            await authorize_actor(self.actor, self.manager.backend.authorize)
            if isinstance(error, ToolError):
                try:
                    data = json.loads(str(error))
                except ValueError:
                    data = None
                failure = ToolFailure(
                    "SERVICE_CALL_FAILED", "Screenshot rendering failed", data=data
                )
            elif isinstance(error, ToolFailure):
                failure = error
            else:
                failure = ToolFailure(
                    "SERVICE_CALL_FAILED",
                    f"Screenshot rendering failed ({type(error).__name__})",
                )
            if not after_write:
                raise failure from error
            result.setdefault("warnings", []).append(
                "Screenshot unavailable after the dashboard write; its recorded write outcome is unchanged."
            )
            result["screenshot_error"] = failure.data
            return result

    async def _capture(
        self,
        result: dict[str, Any],
        url_path: str,
        config: dict[str, Any] | None,
        view_path: str | None,
    ) -> ToolResult:
        await authorize_actor(self.actor, self.manager.backend.authorize)
        if config is None:
            raise ToolFailure(
                "RESOURCE_NOT_FOUND",
                "Dashboard config is unavailable for screenshot view resolution",
                status="not_started",
            )
        target = resolve_dashboard_view(url_path, config, view_path)
        engine, token = await self._target()
        await authorize_actor(self.actor, self.manager.backend.authorize)
        observer = NativeThemeObserver(
            self.manager.hass, self.actor, self.manager.backend.authorize, token=token
        )
        warnings: list[str] = list(target.warnings)
        captures = await await_owned_io(
            capture_dashboard_images(
                target.render_path,
                engine_target=EngineTarget(engine),
                theme_observer=observer,
                capture_warnings=warnings,
            )
        )
        assembled: ToolResult = await await_owned_io(
            self.manager.hass.async_add_executor_job(
                self._assemble, result, captures, target.render_path, warnings
            )
        )
        await authorize_actor(self.actor, self.manager.backend.authorize)
        return assembled

    @staticmethod
    def _assemble(
        result: dict[str, Any],
        captures: list[Any],
        render_path: str,
        warnings: list[str],
    ) -> ToolResult:
        images = tuple(
            ToolImage(capture.data, capture.mime_type) for capture in captures
        )
        result = {
            **result,
            "screenshot_render_path": render_path,
            "screenshots": dashboard_screenshot_metadata(captures, render_path),
        }
        warnings = [*warnings, *dashboard_screenshot_warnings(captures)]
        if warnings:
            result["warnings"] = [*(result.get("warnings") or []), *warnings]
        return ToolResult(result, images=images)
