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
    _SUPPORTED_ENGINES,
    EngineTarget,
    _select_started_verified_engine,
)
from ha_mcp.dashboard_screenshot.theme_guard import addon_credential_from_options
from ha_mcp.runtime.dashboard_routes import (
    DashboardRenderTarget,
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

    async def _discover(self) -> EngineTarget:
        get_supervisor_client = loaded_integration_module(
            self.manager.hass, "hassio"
        ).get_supervisor_client

        client = get_supervisor_client(self.manager.hass)
        async with asyncio.timeout(15):
            await authorize_actor(self.actor, self.manager.backend.authorize)
            apps = await client.addons.list()
            infos = []
            for app in apps:
                if app.slug.endswith(tuple(p.slug_suffix for p in _SUPPORTED_ENGINES)):
                    await authorize_actor(self.actor, self.manager.backend.authorize)
                    info = await client.addons.addon_info(app.slug)
                    infos.append((app.slug, info.to_dict()))
            _slug, selected, profile = _select_started_verified_engine(infos)
        hostname = selected.get("hostname") or selected.get("ip_address")
        if not hostname:
            raise ToolFailure(
                "SERVICE_CALL_FAILED",
                "The screenshot engine app has no reachable hostname",
                status="not_started",
            )
        options = selected.get("options")
        version = selected.get("version")
        return EngineTarget(
            url=f"http://{hostname}:{profile.port}",
            engine_kind=profile.kind,
            engine_version=version.strip()
            if isinstance(version, str) and version.strip()
            else None,
            addon_credential=(
                addon_credential_from_options(
                    options if isinstance(options, dict) else None
                )
                if profile.has_persistent_theme_override
                else None
            ),
        )

    async def _target(self) -> tuple[str, str | None]:
        target = await self._engine_target()
        return (
            target.url,
            target.addon_credential.token if target.addon_credential else None,
        )

    async def _engine_target(self) -> EngineTarget:
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
                return EngineTarget(explicit)
            raise with_recovery_hint(
                ToolFailure(
                    "SERVICE_CALL_FAILED",
                    "No screenshot engine is configured. Set the screenshot engine URL for this entry, or install and start Puppet or WPE Screenshot Engine on Home Assistant OS.",
                    status="not_started",
                ),
                "Set the screenshot engine URL in this entry, or install and start Puppet or WPE Screenshot Engine on Home Assistant OS",
            )
        try:
            discovered = await self._discover()
        except ToolError as error:
            if explicit:
                return EngineTarget(explicit)
            # Discovery is shared with the legacy facade. Normalize its
            # structured refusal here because theme reads also use _target.
            data = json.loads(str(error))
            detail = data["error"]
            failure = ToolFailure(
                detail["code"], detail["message"], data=data, status="not_started"
            )
            hint = (
                detail.get("details")
                or detail.get("suggestion")
                or (
                    "Select one started, schema-verified Puppet or WPE app, or configure "
                    "this entry's screenshot engine URL explicitly."
                )
            )
            raise with_recovery_hint(failure, hint) from None
        except Exception:
            if not explicit:
                raise
            return EngineTarget(explicit)
        # A credential discovered for one app is not evidence about an
        # unrelated explicitly configured renderer.
        return (
            EngineTarget(explicit)
            if explicit and explicit != discovered.url
            else discovered
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

        async def read_config(_name: str) -> dict[str, Any] | None:
            return config

        target = await resolve_dashboard_render_target(
            read_config, dashboard_path=None, url_path=url_path, view_path=view_path
        )
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

        async def read_config(name: str) -> dict[str, Any] | None:
            state = await dashboards.read(name, force=True)
            if state.owner is None and state.url_path != "lovelace":
                raise ToolFailure(
                    "RESOURCE_NOT_FOUND",
                    "Dashboard not found",
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

    def _requested_theme_warnings(self, options: dict[str, Any]) -> list[str]:
        from ha_mcp.runtime.screenshot_theme_validation import (
            _unchecked,
            validate_requested_theme,
        )

        from .native_operations.frontend_themes import theme_image

        warnings: list[str] = []
        theme = options.get("theme")
        if theme is not None and theme != "default":
            try:
                themes = theme_image(self.manager.hass).get("themes")
            except Exception as error:  # noqa: BLE001 - optional Core metadata read
                warnings.extend(_unchecked(theme, str(error)))
            else:
                warnings.extend(
                    validate_requested_theme(
                        themes, theme, options.get("dark_mode", False)
                    )
                )
        return warnings

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
        warnings = self._requested_theme_warnings(options)
        engine = await self._engine_target()
        token = engine.addon_credential.token if engine.addon_credential else None
        observer = NativeThemeObserver(
            self.manager.hass, self.actor, self.manager.backend.authorize, token=token
        )

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
                    engine_target=engine,
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
