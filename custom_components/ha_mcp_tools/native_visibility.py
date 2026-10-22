"""Native visibility uses Core snapshots and the existing enforcement rules."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn, cast

from ha_mcp.errors import create_entity_not_found_error
from ha_mcp.tool_api import CallContext, ToolFailure, ToolResult
from ha_mcp.visibility.enforcement import (
    _FAIL_CLOSED_REASON,
    _HiddenMatcher,
    _inbound_reason,
    _outbound_reason,
    _scan_value,
    _scrub_state_representation,
    _unscannable_reason,
    enforcement_error,
)
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import load_visibility_config
from ha_mcp.visibility.resolver import (
    VisibilityDataUnavailable,
    config_has_active_hide_dimensions,
    config_needs_device_registry,
)

_LOGGER = logging.getLogger(__name__)


def _template_scope_overlaps_hidden(data: Any, hidden: frozenset[str]) -> bool:
    """Use Core's render dependencies, including reads yielding only a scalar."""
    listeners = data.get("listeners") if isinstance(data, dict) else None
    if not hidden or not isinstance(listeners, dict):
        # Condition checks do not provide a RenderInfo; their existing text
        # barrier remains applicable, with its documented attribution limit.
        return False
    if listeners.get("all"):
        return True
    domains = listeners.get("domains") or ()
    return any(entity_id.partition(".")[0] in domains for entity_id in hidden)


def load_native_visibility(directory: Path) -> VisibilityConfig:
    """Only an absent file is disabled; permission/IO failures are not absence."""
    try:
        (directory / "entity_visibility.json").stat()
    except FileNotFoundError:
        return VisibilityConfig()
    return load_visibility_config(directory)


def hidden_from_core(hass: Any, config: VisibilityConfig) -> set[str]:
    """Resolve on HA's loop without invoking any transport or writing exposure."""
    from .websocket_api.assist import _assist_exposure_available, _assist_should_expose
    from .websocket_api.registry import _resolve_registries
    from .websocket_api.visibility import _visibility_hidden_set, _visibility_warnings

    view = _resolve_registries(hass)
    if view.entity is None or (
        config_needs_device_registry(config) and view.device is None
    ):
        raise VisibilityDataUnavailable("Required Core registry is unavailable")
    states = list(hass.states.async_all())
    wire = {**config.to_wire(), "allowlist_authorization": True}
    assist_available = (
        _assist_exposure_available(hass) if config.respect_assist_exposure else True
    )
    hidden = _visibility_hidden_set(
        view,
        states,
        wire,
        lambda entity_id: _assist_should_expose(hass, entity_id, fail_closed=True),
        assist_available=assist_available,
    )
    warnings = _visibility_warnings(
        view, states, wire, assist_available=assist_available
    )
    degraded = [
        warning
        for warning in warnings
        if not warning.startswith(
            "Entity visibility: ignoring unknown exclude_categories"
        )
    ]
    if degraded or view._access_failures:
        raise VisibilityDataUnavailable("Core visibility inputs are incomplete")
    for warning in warnings:
        _LOGGER.warning("%s", warning)
    return hidden


def _native_unscannable_reason(name: str, arguments: dict[str, Any]) -> str | None:
    if name == "ha_manage_app":
        return "App operations can read or act on hidden entities without inspectable Core targets, including opaque HTTP GET APIs."
    if name == "ha_manage_hacs" and arguments.get("action") in {"download", "remove"}:
        return "Installing or removing integration code cannot be constrained to visible entities."
    if name == "ha_get_camera_image":
        return "Camera pixels cannot be filtered for hidden entities under visibility enforcement."
    return None


class NativeVisibility:
    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self.last_good_config: VisibilityConfig | None = None

    async def _config(self, name: str) -> VisibilityConfig | None:
        try:
            config = await self.manager.hass.async_add_executor_job(
                load_native_visibility, self.manager.settings_ui.directory
            )
            self.last_good_config = config
        except (OSError, ValueError):
            _LOGGER.warning(
                "Native visibility config could not be loaded; retaining the last known boundary",
                exc_info=True,
            )
            config = self.last_good_config
        if name == "ha_report_issue" and (
            config is None or not config.restrict_report_issue
        ):
            return None
        if config is None:
            raise ToolFailure(
                "ENTITY_VISIBILITY_ENFORCED",
                "Visibility config is unavailable",
                data=enforcement_error(
                    name, "The visibility configuration could not be loaded."
                ),
                status="not_started",
            )
        return config if config_has_active_hide_dimensions(config) else None

    async def _matcher(self, config: VisibilityConfig) -> _HiddenMatcher:
        try:
            hidden = hidden_from_core(self.manager.hass, config)
        except Exception as error:
            _LOGGER.warning(
                "Native visibility inputs could not be resolved", exc_info=True
            )
            raise VisibilityDataUnavailable(
                "Core visibility inputs are unavailable"
            ) from error
        return cast(
            _HiddenMatcher,
            await self.manager.hass.async_add_executor_job(
                _HiddenMatcher.from_hidden, hidden
            ),
        )

    async def before(
        self, name: str, arguments: dict[str, Any], context: CallContext
    ) -> None:
        config = await self._config(name)
        if config is None:
            return
        if not config.enforce:
            return
        if native_reason := _native_unscannable_reason(name, arguments):
            self._deny(name, native_reason)
        try:
            matcher = await self._matcher(config)
        except VisibilityDataUnavailable:
            self._deny(name, _FAIL_CLOSED_REASON)
        reason = _unscannable_reason(name, arguments)
        if name == "ha_native_llm_call" and matcher.hidden:
            reason = "Imported LLM APIs do not provide an inspectable state-read contract under enforce mode."
        if reason is not None:
            self._deny(name, reason)
        exact, embedded = _scan_value(arguments, matcher)
        if exact is not None:
            raise ToolFailure(
                "ENTITY_NOT_FOUND",
                "Entity not found",
                data=create_entity_not_found_error(exact),
                status="not_started",
            )
        if embedded:
            self._deny(name, _inbound_reason(name))

    @staticmethod
    def _deny(name: str, reason: str) -> NoReturn:
        raise ToolFailure(
            "ENTITY_VISIBILITY_ENFORCED",
            "Entity visibility refused the call",
            data=enforcement_error(name, reason),
            status="not_started",
        )

    async def after(
        self,
        name: str,
        arguments: dict[str, Any],
        context: CallContext,
        result: ToolResult,
    ) -> ToolResult:
        if result.status in {"not_started", "pending", "denied"}:
            return result
        config = await self._config(name)
        if config is None or not config.enforce:
            return result
        if result.images:
            return replace(
                result,
                images=(),
                data=enforcement_error(
                    name,
                    "Images cannot be filtered for hidden entities under visibility enforcement.",
                ),
                is_error=True,
            )
        try:
            matcher = await self._matcher(config)
        except VisibilityDataUnavailable:
            return replace(
                result, data=enforcement_error(name, _FAIL_CLOSED_REASON), is_error=True
            )
        data = result.data
        if name == "ha_eval_template" and _template_scope_overlaps_hidden(
            data, matcher.hidden
        ):
            return replace(
                result,
                data=enforcement_error(name, _outbound_reason(name)),
                is_error=True,
            )
        if name == "ha_get_state":
            attempt = _scrub_state_representation(data, matcher)
            data = attempt.value
            if not attempt.warnable:
                return replace(
                    result,
                    data=enforcement_error(name, _outbound_reason(name)),
                    is_error=True,
                )
        if matcher.embeds(json.dumps(data, default=str)):
            return replace(
                result,
                data=enforcement_error(name, _outbound_reason(name)),
                is_error=True,
            )
        return replace(result, data=data)
