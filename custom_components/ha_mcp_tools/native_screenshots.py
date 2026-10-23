"""Adapt the existing screenshot HTTP engine to HA-scoped discovery and Core themes."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
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
from ha_mcp.runtime.dashboard_routes import (
    DashboardRenderTarget,
    resolve_dashboard_view,
)
from ha_mcp.runtime.screenshot_routes import resolve_dashboard_render_target
from ha_mcp.runtime.screenshot_values import screenshot_result
from ha_mcp.tool_api import CallContext, ToolFailure, ToolImage, ToolResult
from ha_mcp.utils.owned_executor import await_owned_io

from .native_operations.dashboard_storage import DashboardStorage
from .native_operations.registry_access import admit_registry_write, authorize_actor
from .native_operations.registry_failures import with_recovery_hint
from .native_operations.screenshot_theme import NativeThemeObserver
from .native_server_policy import setting_value
from .optional_integrations import loaded_integration_module


@dataclass(slots=True)
class NativeScreenshots:
    manager: Any
    actor: CallContext
    started: bool = field(default=False, init=False)

    def _check_enabled(self) -> None:
        if not self.manager.policy.flag(
            "enable_beta_features"
        ) or not self.manager.policy.flag("enable_dashboard_screenshot"):
            raise ToolFailure(
                "FEATURE_DISABLED",
                "Dashboard screenshot mode is disabled",
                status="not_started",
            )

    async def _authorize_result(self) -> None:
        try:
            await authorize_actor(self.actor, self.manager.backend.authorize)
        except ToolFailure as error:
            if self.started:
                error.status = "outcome_unknown"
            raise

    async def _discover(self) -> tuple[str, str | None]:
        get_supervisor_client = loaded_integration_module(
            self.manager.hass, "hassio"
        ).get_supervisor_client

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
            raise with_recovery_hint(
                ToolFailure(
                    "SERVICE_CALL_FAILED",
                    "No screenshot engine is configured. Set the screenshot engine URL for this entry, or install and start the Puppet app on Home Assistant OS.",
                    status="not_started",
                ),
                "Set the screenshot engine URL in this entry, or install and start the Puppet app on Home Assistant OS",
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
        self.started = False
        try:
            return await self._capture(result, url_path, config, view_path)
        except Exception as error:
            await self._authorize_result()
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
        captures, warnings = await self._render(target, {}, None)
        warnings = [*target.warnings, *warnings]
        assembled: ToolResult = await await_owned_io(
            self.manager.hass.async_add_executor_job(
                self._assemble, result, captures, target.render_path, warnings
            )
        )
        await self._authorize_result()
        return assembled

    async def screenshot(self, **arguments: Any) -> ToolResult:
        self.started = False
        dashboards = DashboardStorage(
            self.manager.hass, self.actor, authorize=self.manager.backend.authorize
        )

        async def read_config(name: str) -> dict[str, Any]:
            state = await dashboards.read(name, force=True)
            if state.config is None:
                raise ToolFailure(
                    "RESOURCE_NOT_FOUND",
                    "Dashboard config is unavailable",
                    status="not_started",
                )
            return state.config

        target = await resolve_dashboard_render_target(
            read_config,
            dashboard_path=arguments.pop("dashboard_path"),
            url_path=arguments.pop("url_path"),
            view_path=arguments.pop("view_path"),
        )
        failures: list[dict[str, Any]] = []
        captures, warnings = await self._render(target, arguments, failures)
        value = screenshot_result(captures, target, failures, warnings)
        if failures:
            value.update(
                operation_outcome="outcome_unknown",
                requires_reread=True,
                retry_performed=False,
            )
            value.setdefault("warnings", []).append(
                "Rendering stopped after a failed viewport. Earlier images are available, but engine-profile effects are uncertain; no request was retried."
            )
        await self._authorize_result()
        return ToolResult(
            value,
            images=tuple(ToolImage(c.data, c.mime_type) for c in captures),
            status="outcome_unknown" if failures else "confirmed",
        )

    async def _render(
        self,
        target: DashboardRenderTarget,
        options: dict[str, Any],
        failures: list[dict[str, Any]] | None,
    ) -> tuple[list[Any], list[str]]:
        from ha_mcp.dashboard_screenshot.capture import validate_capture_parameters

        validate_capture_parameters(**options)
        self.started = False
        await admit_registry_write(self.actor, self.manager.backend.authorize)
        self._check_enabled()
        engine, token = await self._target()
        observer = NativeThemeObserver(
            self.manager.hass, self.actor, self.manager.backend.authorize, token=token
        )
        warnings: list[str] = []

        async def before_request() -> None:
            visibility = getattr(self.manager, "visibility", None)
            if visibility is not None:
                await visibility.before("ha_get_dashboard_screenshot", {}, self.actor)
            await admit_registry_write(self.actor, self.manager.backend.authorize)
            self._check_enabled()
            self.started = True

        async def render() -> list[Any]:
            try:
                return await capture_dashboard_images(
                    target.render_path,
                    **options,
                    engine_target=EngineTarget(engine),
                    theme_observer=observer,
                    capture_warnings=warnings,
                    partial_failures=failures,
                    request_guard=before_request,
                )
            except (Exception, asyncio.CancelledError) as error:
                if isinstance(error, ToolFailure):
                    if self.started:
                        error.status = "outcome_unknown"
                    raise
                if not self.started:
                    raise
                raise ToolFailure(
                    "OUTCOME_UNKNOWN",
                    "Rendering started and may have changed the engine user's profile. No retry was performed.",
                    status="outcome_unknown",
                ) from error

        captures = await await_owned_io(render())
        return captures, warnings

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
