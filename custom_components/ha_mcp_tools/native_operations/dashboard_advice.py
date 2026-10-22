"""Native dashboard advice reads the installed frontend and admitted resources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ha_mcp.runtime.dashboard_card_values import describe_card_result
from ha_mcp.runtime.skill_content import apply_skill_content
from ha_mcp.runtime.skill_files import read_skill_files
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.owned_executor import await_owned_io

from .lovelace_resources import NativeResources, resources_need_loading
from .registry_access import authorize_actor


def dashboard_read_writes(hass: Any, arguments: dict[str, Any]) -> bool:
    card_type = arguments.get("card_type") or ""
    return bool(
        arguments.get("describe")
        and (not card_type or card_type.startswith("custom:"))
        and resources_need_loading(hass)
    )


@dataclass(slots=True)
class DashboardAdvice:
    manager: Any
    actor: CallContext

    async def resource_items(
        self, *, allow_initialize: bool = True
    ) -> list[dict[str, Any]]:
        resources = NativeResources(
            self.manager.hass, self.actor, authorize=self.manager.backend.authorize
        )
        return await resources.inspection_items(allow_initialize=allow_initialize)

    async def describe(self, card_type: str | None) -> dict[str, Any]:
        from ..card_descriptions import async_describe_card

        items = (
            await self.resource_items()
            if not card_type or card_type.startswith("custom:")
            else []
        )
        value = await async_describe_card(
            self.manager.hass, card_type, resource_items=items
        )
        await authorize_actor(self.actor, self.manager.backend.authorize)
        return describe_card_result(value, card_type)

    async def warnings(self, config: dict[str, Any]) -> list[str]:
        from ..card_definitions import _cards, async_card_warnings

        custom = any(
            str(card.get("type", "")).startswith("custom:")
            for _, card in _cards(config)
        )
        try:
            items = await self.resource_items(allow_initialize=False) if custom else []
        except ToolFailure as error:
            if error.code != "RESOURCE_NOT_READY":
                raise
            return [
                "Custom-card checks were not run because dashboard resources are not loaded. List dashboard resources with write admission first; this advisory check did not start a migration after the saved dashboard hash was read."
            ]
        return await async_card_warnings(
            self.manager.hass, config, resource_items=items
        )

    async def attach_skill(self, result: dict[str, Any], requested: bool) -> None:
        enabled = self.manager.policy.flag("enable_mandatory_bps")
        files = ["references/dashboard-guide.md"] if requested else []
        values = await await_owned_io(
            self.manager.hass.async_add_executor_job(
                lambda: read_skill_files(files, enabled=enabled)
            )
        )
        apply_skill_content(
            result,
            values["content"],
            enabled=enabled,
            requested=requested,
            vendor_missing=values["vendor_missing"],
        )

    async def screenshot(
        self,
        result: dict[str, Any],
        url_path: str,
        config: dict[str, Any] | None,
        view_path: str | None,
        *,
        requested: bool,
        after_write: bool,
    ) -> Any:
        if not requested:
            return result
        if not self.manager.policy.flag(
            "enable_beta_features"
        ) or not self.manager.policy.flag("enable_dashboard_screenshot"):
            result.setdefault("warnings", []).append(
                "Screenshot requested but dashboard screenshot mode is disabled. Enable the 'dashboard screenshot' beta feature to use it."
            )
            return result
        from ..native_screenshots import NativeScreenshots

        return await NativeScreenshots(self.manager, self.actor).capture(
            result, url_path, config, view_path, after_write=after_write
        )
