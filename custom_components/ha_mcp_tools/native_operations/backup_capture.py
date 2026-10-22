"""On-demand edit snapshots read the actual domain owners, without starting flows."""

import json
from typing import Any

from homeassistant.helpers.json import json_bytes

from ha_mcp.runtime.helper_schema import SIMPLE_HELPER_TYPES
from ha_mcp.runtime.snapshot_payloads import _subentry_target
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation
from ha_mcp.utils.owned_executor import await_owned_io

from .registry_access import authorize_actor


class NativeCaptureSources:
    def __init__(self, manager: Any, actor: CallContext) -> None:
        self.manager, self.actor = manager, actor
        self.hass = manager.hass
        self.domains = manager.snapshots.domains

    async def read(self, domain: str, identifier: str) -> tuple[str, Any]:
        await authorize_actor(self.actor, self.manager.backend.authorize)
        if domain.startswith("helper_"):
            return await self._helper(domain.removeprefix("helper_"), identifier)
        readers = {
            "automation": self._configuration,
            "script": self._configuration,
            "scene": self._configuration,
            "entity": self._identity,
            "entity_registry": self._identity,
            "entity_exposure": self._identity,
            "device": self._identity,
            "label": self._identity,
            "category": self._registry,
            "area_or_floor": self._registry,
            "group": self._group,
            "zone": self._zone,
            "dashboard": self._dashboard,
            "dashboard_resource": self._resource,
            "blueprint_automation": self._blueprint,
            "blueprint_script": self._blueprint,
            "file": self._file,
            "yaml": self._file,
            "yaml_file": self._file,
            "integration": self._integration,
            "todo_item": self._todo,
            "calendar_event": self._calendar,
        }
        reader = readers.get(domain)
        if reader is None:
            raise UnsupportedOperation(f"Backup domain {domain}")
        return await reader(domain, identifier)

    async def _configuration(self, domain: str, identifier: str) -> tuple[str, Any]:
        from .config_reads import _automation_read, _scene_read, _script_read

        reader = {
            "automation": _automation_read,
            "scene": _scene_read,
            "script": _script_read,
        }[domain]
        value = await await_owned_io(reader(self.hass, {"identifier": identifier}))
        return value.get("storage_key") or identifier, json.loads(
            json_bytes(value["config"])
        )

    async def _identity(self, domain: str, identifier: str) -> tuple[str, Any]:
        if domain == "entity_registry":
            from homeassistant.helpers import entity_registry as er

            row = er.async_get(self.hass).async_get(identifier)
            if row is None:
                return identifier, None
            identifier = row.id
        factory = {
            "entity": self.domains.states,
            "entity_registry": self.domains.entities,
            "entity_exposure": self.domains.legacy_exposure,
            "device": self.domains.devices,
            "label": self.domains.labels,
        }[domain]
        return identifier, await factory(self.actor).read_snapshot(identifier)

    async def _registry(self, domain: str, identifier: str) -> tuple[str, Any]:
        if domain == "category":
            from .registry_mutations import category_read

            scope, separator, key = identifier.rpartition(":")
            if not separator:
                raise ValueError("Category backup identity must be scope:category_id")
            row = await category_read(self.hass, {"scope": scope, "category_id": key})
            return identifier, {"scope": scope, **row} if row is not None else None
        from .area_floor_mutations import area_read, floor_read

        kind, separator, key = identifier.partition(":")
        if not separator or kind not in {"area", "floor"}:
            raise ValueError("Area/floor backup identity must be area:id or floor:id")
        row = await (area_read if kind == "area" else floor_read)(
            self.hass, {f"{kind}_id": key}
        )
        return identifier, {"kind": kind, **row} if row is not None else None

    async def _group(self, domain: str, identifier: str) -> tuple[str, Any]:
        from .group_records import component, group_snapshot, public_snapshot, target

        entity_id = (
            identifier if identifier.startswith("group.") else f"group.{identifier}"
        )
        return identifier, public_snapshot(
            group_snapshot(target(component(self.hass), entity_id))
        )

    async def _zone(self, domain: str, identifier: str) -> tuple[str, Any]:
        return await self._storage("zone", identifier)

    async def _dashboard(self, domain: str, identifier: str) -> tuple[str, Any]:
        from .dashboard_storage import DashboardStorage

        owner = DashboardStorage(
            self.hass, self.actor, authorize=self.manager.backend.authorize
        )
        return identifier, (await owner.read(identifier)).snapshot

    async def _resource(self, domain: str, identifier: str) -> tuple[str, Any]:
        rows = await self.domains.resources(self.actor).inspection_items()
        return identifier, next(
            (row for row in rows if row.get("id") == identifier), None
        )

    async def _blueprint(self, domain: str, identifier: str) -> tuple[str, Any]:
        from .blueprint_owner import BlueprintOwner

        owner = BlueprintOwner(
            self.hass,
            self.actor,
            domain.removeprefix("blueprint_"),
            authorize=self.manager.backend.authorize,
        )
        return identifier, (await owner.read(identifier)).text

    async def _file(self, domain: str, identifier: str) -> tuple[str, Any]:
        from .file_restore import NativeFileRestore

        # Only current() runs. The empty saved text is inactive; the real file
        # identity and normal File/YAML entry and path controls remain required.
        owner = NativeFileRestore(self.manager, self.actor, domain, identifier, "")
        return identifier, await owner.current()

    async def _integration(self, domain: str, identifier: str) -> tuple[str, Any]:
        from .flow_records import entry_metadata

        if self.hass.config_entries.async_get_entry(identifier) is None:
            return identifier, None
        return identifier, json.loads(
            json_bytes(await entry_metadata(self.hass, {"entry_id": identifier}))
        )

    async def _todo(self, domain: str, identifier: str) -> tuple[str, Any]:
        from ha_mcp.runtime.todo_targets import todo_target

        from .todo import NativeTodoOperations

        entity_id, separator, reference = identifier.partition("::")
        if not separator or not reference:
            raise ValueError("Todo backup identity must be entity_id::item")
        items = NativeTodoOperations(self.manager.backend)._item_list(entity_id)
        target = todo_target(items, reference)
        return identifier, {"todo_entity_id": entity_id, **target["current_item"]}

    async def _calendar(self, domain: str, identifier: str) -> tuple[str, Any]:
        from .calendar_events import NativeCalendars

        owner = NativeCalendars(
            self.hass, self.actor, authorize=self.manager.backend.authorize
        )
        return identifier, await owner.read_snapshot(identifier)

    async def _storage(self, helper_type: str, identifier: str) -> tuple[str, Any]:
        from homeassistant.helpers import entity_registry as er

        from ..helper_collections import read_item

        is_entity = identifier.startswith(f"{helper_type}.")
        result = read_item(
            self.hass,
            er.async_get(self.hass),
            helper_type,
            identifier if is_entity else None,
            None if is_entity else identifier,
        )
        if not result.get("success"):
            if result.get("error", {}).get("code") == "not_found":
                return identifier, None
            raise UnsupportedOperation(f"Stored {helper_type} snapshot")
        return identifier, await self.manager.snapshots._helper_config(
            {
                "helper_type": helper_type,
                "current_item": result["item"],
                "entity_id": result.get("entity_id"),
            }
        )

    async def _helper(self, helper_type: str, identifier: str) -> tuple[str, Any]:
        from .snapshot_sources import flow_snapshot, subentry_snapshot

        if helper_type in SIMPLE_HELPER_TYPES:
            return await self._storage(helper_type, identifier)
        if helper_type == "config_subentry":
            entry_id, subentry_id = _subentry_target(identifier)
            return identifier, await subentry_snapshot(
                self.hass, {"entry_id": entry_id, "subentry_id": subentry_id}
            )
        from homeassistant.helpers import entity_registry as er
        from homeassistant.loader import async_get_config_flows

        types = await await_owned_io(
            async_get_config_flows(self.hass, type_filter="helper")
        )
        if helper_type not in types:
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                "Home Assistant does not declare this helper flow type",
                status="not_started",
            )
        entry = self.hass.config_entries.async_get_entry(identifier)
        if entry is None:
            row = er.async_get(self.hass).async_get(identifier)
            entry = (
                self.hass.config_entries.async_get_entry(row.config_entry_id)
                if row and row.config_entry_id
                else None
            )
        if entry is None:
            return identifier, None
        if entry.domain != helper_type:
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                "The selected entry is not this helper type",
                status="not_started",
            )
        return entry.entry_id, await flow_snapshot(
            self.hass, {"helper_type": helper_type, "entry_id": entry.entry_id}
        )
