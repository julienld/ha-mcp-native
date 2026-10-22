"""Live snapshot reads and writes over the native semantic backend."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

from ha_mcp.runtime.helper_schema import SIMPLE_HELPER_TYPES
from ha_mcp.runtime.snapshot_files import _flow_entry_id, _is_flow_helper_domain
from ha_mcp.runtime.snapshot_payloads import (
    BackupRestoreError,
    InvalidBackupSnapshotError,
    _flow_options,
    _FlowHelperReadError,
    _strip_readonly,
    validate_snapshot_identity,
)
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation

from .native_operations.snapshot_sources import (
    flow_snapshot,
    optional_subentry_snapshot,
)


@dataclass
class StorageBefore:
    snapshot: dict[str, Any] | None
    item: dict[str, Any] | None
    registry: dict[str, Any]


def config_storage_key(data: dict[str, Any]) -> str:
    """A snapshot names a stored config, never a newly reassigned entity alias."""
    domain, config = data["domain"], data["config"]
    if not isinstance(config, dict):
        raise InvalidBackupSnapshotError("Stored configuration must be an object")
    key = config.get("id") if domain in {"automation", "scene"} else None
    if key is None:
        key = data["entity_id"].removeprefix(f"{domain}.")
    if not isinstance(key, str) or not key:
        raise InvalidBackupSnapshotError("Stored configuration has no item identity")
    return key


class NativeSnapshotDomains:
    """A stored snapshot identifies an operation; it never supplies a URL or path."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager

    def entry_revision(self, data: dict[str, Any]) -> float | None:
        """Read the revision alongside the before-image, before safety I/O awaits."""
        from ha_mcp.runtime.snapshot_payloads import _subentry_target

        entry_id = (
            _subentry_target(data["entity_id"])[0]
            if data["domain"] == "helper_config_subentry"
            else _flow_entry_id(
                data["entity_id"],
                data["config"],
                data["domain"].removeprefix("helper_"),
            )
        )
        entry = self.manager.hass.config_entries.async_get_entry(entry_id)
        return entry.modified_at.timestamp() if entry is not None else None

    async def current(self, data: dict[str, Any], actor: CallContext) -> Any:
        validate_snapshot_identity(data)
        await self.manager.backend.authorize(actor)
        domain = data["domain"]
        if domain in {"automation", "script", "scene"}:
            from homeassistant.helpers.json import json_bytes

            from .native_operations.core_views import read_config_item

            current = await read_config_item(
                self.manager.hass, domain, config_storage_key(data)
            )
            # Core's YAML owner returns annotated nodes. Cross the same public
            # value boundary as NativeBackend before diffing or writing a snapshot.
            return json.loads(json_bytes(current))
        if domain == "label":
            config = data["config"]
            if (
                not isinstance(config, dict)
                or config.get("label_id") != data["entity_id"]
            ):
                raise InvalidBackupSnapshotError(
                    "Label snapshot does not match its identity"
                )
            return await self.labels(actor).read_snapshot(data["entity_id"])
        if domain in {"category", "area_or_floor"}:
            from ha_mcp.runtime.snapshot_registry_restore import current_registry

            return await current_registry(
                self.manager.backend, actor, domain, data["entity_id"], data["config"]
            )
        if domain == "todo_item":
            from ha_mcp.runtime.snapshot_todo import current_todo

            return await current_todo(
                self.manager.backend, actor, data["entity_id"], data["config"]
            )
        if domain == "helper_config_subentry":
            return await self._subentry(data)
        if _is_flow_helper_domain(domain):
            return await self._flow(data)
        return (await self.storage_before(data, actor)).snapshot

    def labels(self, actor: CallContext) -> Any:
        from .native_operations.labels import NativeLabels

        return NativeLabels(
            self.manager.hass, actor, authorize=self.manager.backend.authorize
        )

    async def restore_label(
        self, data: dict[str, Any], before: Any, actor: CallContext
    ) -> dict[str, Any]:
        try:
            result: dict[str, Any] = await self.labels(actor).restore_snapshot(
                data["entity_id"], data["config"], before
            )
            return result
        except ToolFailure as error:
            if error.code != "RESOURCE_NOT_FOUND":
                raise
            raise BackupRestoreError(
                "The saved label no longer exists. This restore updates existing labels; "
                "it cannot recreate the original label identity.",
                reason="entry_deleted",
            ) from error

    async def storage_before(
        self, data: dict[str, Any], actor: CallContext
    ) -> StorageBefore:
        domain = data["domain"]
        helper_type = "zone" if domain == "zone" else domain.removeprefix("helper_")
        if helper_type not in SIMPLE_HELPER_TYPES or domain not in {
            "zone",
            f"helper_{helper_type}",
        }:
            raise UnsupportedOperation(f"snapshots.{domain}.read")
        config = data["config"]
        stored_id = config.get("id") if isinstance(config, dict) else None
        if not isinstance(stored_id, str) or not stored_id:
            raise InvalidBackupSnapshotError(
                "Stored helper snapshot has no item identity"
            )
        return await self._storage(helper_type, stored_id, actor, data["config"])

    async def _storage(
        self, helper_type: str, target: str, actor: CallContext, config: dict[str, Any]
    ) -> StorageBefore:
        from homeassistant.helpers import entity_registry as er

        result = await self.manager.backend.execute(
            "helpers.storage.read",
            {
                "helper_type": helper_type,
                "item_id": target,
            },
            actor,
        )
        if not result.get("success"):
            if result.get("error", {}).get("code") == "not_found":
                return StorageBefore(
                    None,
                    None,
                    {
                        "entity_id": None,
                        "id": None,
                        "platform": helper_type,
                        "unique_id": target,
                        "values": {},
                    },
                )
            raise UnsupportedOperation(f"snapshots.helper_{helper_type}.read")
        raw = copy.deepcopy(result["item"])
        item = dict(raw)
        entity_id = result.get("entity_id")
        row = (
            er.async_get(self.manager.hass).async_get(entity_id) if entity_id else None
        )
        item["registry_icon"] = row.icon if row else None
        if helper_type == "tag" and row is not None:
            item["name"] = row.name
        values = (
            {"icon": row.icon if row else None} if "registry_icon" in config else {}
        )
        if helper_type == "tag":
            values["name"] = row.name if row else None
        expected_registry = {
            "entity_id": entity_id,
            "id": getattr(row, "id", None),
            "platform": helper_type,
            "unique_id": target,
            "values": values,
        }
        return StorageBefore(item, raw, expected_registry)

    async def _flow(self, data: dict[str, Any]) -> Any:
        helper_type = data["domain"].removeprefix("helper_")
        target = _flow_entry_id(data["entity_id"], data["config"], helper_type)
        _flow_options(data["config"].get("options"), helper_type)
        try:
            return await flow_snapshot(
                self.manager.hass, {"helper_type": helper_type, "entry_id": target}
            )
        except _FlowHelperReadError as error:
            if error.reason == "entry_missing":
                return None
            raise

    async def _subentry(self, data: dict[str, Any]) -> Any:
        from ha_mcp.runtime.snapshot_payloads import _subentry_target

        entry_id, subentry_id = _subentry_target(data["entity_id"])
        config = data["config"]
        if not isinstance(config, dict) or (
            config.get("entry_id"),
            config.get("subentry_id"),
        ) != (entry_id, subentry_id):
            raise InvalidBackupSnapshotError(
                "Subentry snapshot does not match its target"
            )
        return await optional_subentry_snapshot(
            self.manager.hass,
            {
                "entry_id": entry_id,
                "subentry_id": subentry_id,
                "subentry_type": config.get("subentry_type"),
            },
        )

    async def apply_config(
        self, data: dict[str, Any], before: Any, actor: CallContext
    ) -> Any:
        domain = data["domain"]
        key = config_storage_key(data)
        return await self.manager.backend.execute(
            f"config.{domain}.write",
            {
                "storage_key": key,
                "identifier": key,
                "expected_config": before,
                "data": copy.deepcopy(data["config"]),
            },
            actor,
        )

    async def apply_flow(
        self,
        data: dict[str, Any],
        actor: CallContext,
        *,
        needs_safety: bool,
        has_safety: bool,
        revision: float | None,
    ) -> Any:
        from ha_mcp.runtime.flows.settings import FlowSettings, bind_flow_settings
        from ha_mcp.runtime.snapshot_flow_restore import (
            _recreate_flow_helper,
            _restore_config_subentry,
            _restore_flow_helper,
        )

        from .native_snapshot_flows import NativeSnapshotFlowPorts

        ports = NativeSnapshotFlowPorts(self.manager, actor, revision)
        with bind_flow_settings(
            FlowSettings(lambda: self.manager.policy.flag("redact_secrets"))
        ):
            if data["domain"] == "helper_config_subentry":
                return await _restore_config_subentry(
                    ports,
                    data["entity_id"],
                    data["config"],
                    recreate_only=not has_safety,
                )
            helper_type = data["domain"].removeprefix("helper_")
            invoke = _restore_flow_helper if needs_safety else _recreate_flow_helper
            return await invoke(ports, data["entity_id"], data["config"], helper_type)

    async def apply_storage(
        self, data: dict[str, Any], before: StorageBefore, actor: CallContext
    ) -> dict[str, Any]:
        """Restore an existing stored item, comparing the safety capture before commit."""
        helper_type = (
            "zone"
            if data["domain"] == "zone"
            else data["domain"].removeprefix("helper_")
        )
        if helper_type in {"zone", "tag"}:
            from ha_mcp.runtime.snapshot_storage_restore import (
                restore_tag,
                restore_zone,
            )

            from .native_snapshot_storage import NativeStoredRestorePorts

            invoke = restore_zone if helper_type == "zone" else restore_tag
            return await invoke(
                NativeStoredRestorePorts(self.manager, actor, helper_type, before),
                data["config"]["id"],
                _strip_readonly(data["config"], "id"),
            )
        if helper_type not in SIMPLE_HELPER_TYPES:
            raise UnsupportedOperation(f"snapshots.{data['domain']}.restore")
        config = data["config"]
        if not isinstance(config, dict) or not config.get("id"):
            raise InvalidBackupSnapshotError(
                "Stored helper snapshot has no item identity"
            )
        if before.item is None:
            raise ToolFailure(
                "RESOURCE_NOT_FOUND",
                "Stored helper no longer exists",
                status="not_started",
            )
        payload = _strip_readonly(config, "id", "registry_icon")
        registry = (
            {"icon": config["registry_icon"]} if "registry_icon" in config else {}
        )
        result: dict[str, Any] = await self.manager.backend.execute(
            "helpers.storage.write",
            {
                "helper_type": helper_type,
                "action": "update",
                "item_id": str(config["id"]),
                "data": payload,
                "registry": registry,
                "expected_item": before.item,
                "expected_registry": before.registry,
            },
            actor,
        )
        if not result.get("success"):
            raise ToolFailure(
                "RESTORE_FAILED",
                "Core rejected the stored helper restore",
                status="not_started",
            )
        item = result["item"]
        return (
            {**item, "warnings": result["warnings"]} if result.get("warnings") else item
        )
