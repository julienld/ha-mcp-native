"""Read a detached Core overview and project it without a loopback client."""

from dataclasses import dataclass
from functools import partial
from typing import Any

from homeassistant.helpers.json import json_bytes

from ha_mcp.runtime.overview_results import OverviewResultBuilder
from ha_mcp.runtime.overview_sections import (
    apply_overview_notifications,
    apply_overview_repairs,
    apply_overview_system_info,
)
from ha_mcp.runtime.overview_values import (
    _OVERVIEW_AVAILABLE_FIELDS,
    _OVERVIEW_NOTIFICATION_FIELDS,
    _OVERVIEW_REPAIR_FIELDS,
    _OverviewInputs,
    resolve_overview_request,
)
from ha_mcp.runtime.projection import project_fields
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.owned_executor import await_owned_io

from ..native_visibility import hidden_from_core
from .registry_access import authorize_actor


def _project_sections(
    result: dict[str, Any],
    raw: dict[str, Any],
    inputs: _OverviewInputs,
    requested: set[str] | frozenset[str],
    errors: set[str],
    base_url: str | None,
    independent: bool,
) -> None:
    if "system_info" in requested:
        apply_overview_system_info(
            result, raw.get("config", {}), base_url, inputs.detail_level
        )
        if "config" in errors:
            result.setdefault("warnings", []).append(
                "system info unavailable: Core configuration could not be read"
            )
        elif inputs.detail_level == "full":
            result["system_info"] = {**raw.get("config", {}), **result["system_info"]}
    if requested & _OVERVIEW_NOTIFICATION_FIELDS:
        if inputs.include_notifications:
            apply_overview_notifications(result, raw.get("notifications", []))
            if "notifications" in errors:
                result.setdefault("warnings", []).append(
                    "notifications unavailable: Core read failed"
                )
        elif independent:
            result.setdefault("warnings", []).append(
                "notifications omitted: include_notifications=False"
            )
    if requested & _OVERVIEW_REPAIR_FIELDS:
        apply_overview_repairs(
            result, raw.get("repairs", []), inputs.include_dismissed_repairs
        )
        if "repairs" in errors:
            result["repairs_error"] = "Could not fetch repairs: Core read failed"


def _project_overview(
    snapshot: bytes,
    inputs: _OverviewInputs,
    fields: list[str] | None,
    independent: bool,
    base_url: str | None,
) -> dict[str, Any]:
    import json

    raw: dict[str, Any] = json.loads(snapshot)
    errors = set(raw.get("slice_errors", []))
    requested = set(fields or ()) if independent else _OVERVIEW_AVAILABLE_FIELDS
    if independent:
        result: dict[str, Any] = {"success": True}
    else:
        if "states" in errors:
            raise ToolFailure("CORE_READ_FAILED", "Core states are unavailable.")
        result = OverviewResultBuilder()._build_overview_image(
            raw["states"],
            raw["services"],
            raw["area_registry"],
            raw["entity_registry"],
            raw["device_registry"],
            detail_level=inputs.detail_level,
            max_entities_per_domain=inputs.max_entities_per_domain,
            include_state=inputs.include_state,
            include_entity_id=inputs.include_entity_id,
            domains_filter=inputs.domains_filter,
            limit=inputs.limit,
            offset=inputs.offset,
            partial_warnings=[
                f"Core {name.replace('_', ' ')} unavailable"
                for name in sorted(
                    errors
                    & {
                        "services",
                        "area_registry",
                        "entity_registry",
                        "device_registry",
                    }
                )
            ],
            visibility_warnings=[],
        )
    _project_sections(result, raw, inputs, requested, errors, base_url, independent)
    return project_fields(result, fields, available_fields=_OVERVIEW_AVAILABLE_FIELDS)


@dataclass(slots=True)
class NativeOverview:
    manager: Any
    actor: CallContext

    async def _hidden(self) -> frozenset[str]:
        config = await self.manager.visibility._config("ha_get_overview")
        await authorize_actor(self.actor, self.manager.backend.authorize)
        return (
            frozenset(hidden_from_core(self.manager.hass, config))
            if config is not None
            else frozenset()
        )

    def _capture(
        self, inputs: _OverviewInputs, fields: list[str] | None, independent: bool
    ) -> dict[str, Any]:
        from ..websocket_api import overview

        wanted = set(fields or ())
        include_notifications = inputs.include_notifications and (
            not independent or bool(wanted & _OVERVIEW_NOTIFICATION_FIELDS)
        )
        include_repairs = not independent or bool(wanted & _OVERVIEW_REPAIR_FIELDS)
        if independent:
            result: dict[str, Any] = {"slice_errors": []}
            reads = {
                **(
                    {"config": self.manager.hass.config.as_dict}
                    if "system_info" in wanted
                    else {}
                ),
                **(
                    {
                        "notifications": partial(
                            overview._overview_notifications, self.manager.hass
                        )
                    }
                    if include_notifications
                    else {}
                ),
                **(
                    {"repairs": partial(overview._overview_repairs, self.manager.hass)}
                    if include_repairs
                    else {}
                ),
            }
            for name, read in reads.items():
                try:
                    result[name] = read()
                except Exception:  # noqa: BLE001 - per-slice degradation is public behavior
                    result["slice_errors"].append(name)
            return result
        result = overview._do_overview(
            self.manager.hass,
            {
                "include_notifications": include_notifications,
                "include_repairs": include_repairs,
            },
        )
        # Full Core config retains fields added after the bridge's historical projection.
        try:
            result["config"] = self.manager.hass.config.as_dict()
        except Exception:  # noqa: BLE001 - the other slices remain useful
            result["slice_errors"].append("config")
        return result

    def _base_url(self) -> str | None:
        from homeassistant.helpers.network import NoURLAvailableError, get_url

        try:
            url: str = get_url(self.manager.hass)
            return url
        except NoURLAvailableError:
            return None

    async def get_overview(self, **arguments: Any) -> dict[str, Any]:
        await authorize_actor(self.actor, self.manager.backend.authorize)
        inputs, fields, independent = resolve_overview_request(**arguments)
        hidden = await self._hidden() if not independent else frozenset()
        image = self._capture(inputs, fields, independent)
        if not independent:
            image["states"] = [
                row for row in image["states"] if row["entity_id"] not in hidden
            ]
        base_url = self._base_url()
        detached = json_bytes(image)
        result: dict[str, Any] = await await_owned_io(
            self.manager.hass.async_add_executor_job(
                partial(
                    _project_overview, detached, inputs, fields, independent, base_url
                )
            )
        )
        if not independent and await self._hidden() != hidden:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Entity visibility changed while the overview was projected.",
            )
        await authorize_actor(self.actor, self.manager.backend.authorize)
        if base_url:
            result["settings_url"] = base_url.rstrip("/") + "/api/ha_mcp_tools/ui/boot"
        else:
            result["settings_url_hint"] = (
                "Open Home Assistant's HA-MCP panel at /api/ha_mcp_tools/ui/boot to configure tools and feature flags."
            )
        if self.actor.read_only or self.manager.policy.read_only:
            result["read_only_mode"] = True
            result["read_only_mode_hint"] = (
                "This connection is read-only. Changes require the user's authorization and the configured Home Assistant policy."
            )
        if self.manager.policy.flag("enable_tool_search"):
            result["tool_discovery"] = {
                "hint": "Use ha_search_tools to discover tools and their input schemas, then call a discovered tool by name or through the matching read/write/delete proxy.",
                "pinned_tools": sorted(
                    name
                    for name in self.manager.policy.pinned_tools
                    if name in self.manager.engine.tools
                    and self.manager.policy.tool_enabled(
                        self.manager.engine.tools[name]
                    )
                    and (
                        self.actor.allowed_tools is None
                        or name in self.actor.allowed_tools
                    )
                ),
            }
        return result
