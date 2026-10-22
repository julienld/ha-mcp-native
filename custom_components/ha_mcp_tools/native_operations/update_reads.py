"""Read update state and original provider notes, keeping actor and owner live."""

import asyncio
import json
from typing import Any

from homeassistant.helpers.json import json_bytes

from ha_mcp.runtime.update_values import update_details, update_listing
from ha_mcp.tool_api import ToolFailure
from ha_mcp.utils.owned_executor import await_owned_io
from ha_mcp.utils.release_notes import (
    _fetch_core_release_notes,
    _fetch_github_release_notes,
    _fetch_release_data,
)

from ..native_visibility import hidden_from_core
from .registry_access import authorize_actor
from .update_entities import check_update_target, core_update_category, update_target


class UpdateReads:
    manager: Any
    actor: Any

    async def _authorize(self) -> None:
        await authorize_actor(self.actor, self.manager.backend.authorize)

    async def _hidden(self) -> set[str]:
        visibility = getattr(self.manager, "visibility", None)
        config = (
            await visibility._config("ha_manage_updates")
            if visibility is not None
            else None
        )
        await self._authorize()
        return (
            hidden_from_core(self.manager.hass, config) if config is not None else set()
        )

    async def _visible_target(self, entity_id: str) -> Any:
        if entity_id in await self._hidden():
            raise ToolFailure(
                "ENTITY_NOT_FOUND", "Update entity not found", status="not_started"
            )
        return update_target(self.manager.hass, entity_id)

    async def _list(self, include_skipped: bool) -> dict[str, Any]:
        from homeassistant.components.update import DATA_COMPONENT

        hidden = await self._hidden()
        rows = json.loads(
            json_bytes(
                [
                    state.as_dict()
                    for state in self.manager.hass.states.async_all("update")
                    if state.entity_id not in hidden
                ]
            )
        )
        component = self.manager.hass.data.get(DATA_COMPONENT)
        categories = {}
        if component is not None:
            for row in rows:
                category = core_update_category(
                    self.manager.hass, component.get_entity(row["entity_id"])
                )
                if category is not None:
                    categories[row["entity_id"]] = category
        return update_listing(
            rows, include_skipped, include_attributes=True, native_categories=categories
        )

    async def _get(self, entity_id: str, include_release_notes: bool) -> dict[str, Any]:
        target = await self._visible_target(entity_id)
        state = self.manager.hass.states.get(entity_id)
        record = json.loads(json_bytes(state.as_dict()))
        result = update_details(record, include_attributes=True)
        result["category"] = (
            core_update_category(self.manager.hass, target.entity) or result["category"]
        )
        notes, source = await self._notes(target, result)
        if notes:
            result.update(release_notes=notes, release_notes_source=source)
        elif result.get("release_url"):
            result["release_notes_hint"] = (
                "Release notes could not be fetched automatically. "
                f"View them at: {result['release_url']}"
            )
        if include_release_notes and result["category"] == "core":
            await self._multi_version(result)
        current = await self._visible_target(entity_id)
        check_update_target(self.manager.hass, target)
        if current.entity.latest_version != record["attributes"].get("latest_version"):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The offered version changed while reading release notes; read the current update again.",
                status="not_started",
            )
        return result

    async def _notes(
        self, target: Any, result: dict[str, Any]
    ) -> tuple[Any, str | None]:
        from homeassistant.components.update import UpdateEntityFeature

        notes, source = None, None
        if (
            UpdateEntityFeature.RELEASE_NOTES in target.entity.supported_features
            and target.entity.available
        ):
            try:
                async with asyncio.timeout(30):
                    notes = await target.entity.async_release_notes()
                if notes:
                    source = "core"
            except Exception:  # noqa: BLE001 - provider notes are best-effort enrichment
                # Release-note enrichment is best effort, as in the legacy tool.
                # No error object (which may contain an authenticated URL) escapes.
                pass
        if not notes and result.get("release_url"):
            fetched = await await_owned_io(
                _fetch_github_release_notes(result["release_url"])
            )
            if fetched:
                notes, source = fetched["notes"], fetched["source"]
        if not notes and result["category"] == "core":
            fetched = await await_owned_io(
                _fetch_core_release_notes(result["latest_version"])
            )
            if fetched:
                notes, source = fetched["notes"], fetched["source"]
        return notes, source

    async def _multi_version(self, result: dict[str, Any]) -> None:
        installed, latest = result["installed_version"], result["latest_version"]
        if installed and latest:
            release = await await_owned_io(_fetch_release_data(installed, latest))
            result.update(
                installed_integrations=sorted(
                    {
                        entry.domain
                        for entry in self.manager.hass.config_entries.async_entries()
                    }
                ),
                multi_version_release_notes=release.get("release_notes", []),
                breaking_changes={
                    "entries": release.get("entries", []),
                    "count": release.get("count", 0),
                    "versions_checked": release.get("versions_checked", []),
                },
            )
