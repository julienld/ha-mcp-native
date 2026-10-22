"""Capture the resolved helper's pre-write state without a network client."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from ha_mcp.runtime.snapshot_payloads import _FlowHelperReadError
from ha_mcp.runtime.snapshot_store import SnapshotStore
from ha_mcp.tool_api import CallContext
from ha_mcp.utils.owned_executor import await_owned_io

from .const import SERVER_CONFIG_SUBDIR
from .native_server_policy import environment_name

_LOGGER = logging.getLogger(__name__)


class NativeSnapshots:
    def __init__(self, manager: Any) -> None:
        from .native_snapshot_domains import NativeSnapshotDomains

        self.manager = manager
        self.domains = NativeSnapshotDomains(manager)
        self.pending: set[asyncio.Future[Any]] = set()
        self.stores: dict[Path, SnapshotStore] = {}

    def _value(self, name: str) -> Any:
        from ha_mcp.config_settings import Settings

        return os.environ.get(
            environment_name(name),
            self.manager.policy.flags.get(name, Settings.model_fields[name].default),
        )

    def _store(self) -> SnapshotStore:
        configured = str(self._value("auto_backup_dir") or "").strip()
        directory = (
            Path(configured).expanduser()
            if configured
            else Path(self.manager.hass.config.path(SERVER_CONFIG_SUBDIR)) / "backups"
        )
        store = self.stores.setdefault(directory, SnapshotStore(directory))
        store.retention = max(1, int(self._value("auto_backup_retain_per_entity")))
        store.throttle_seconds = max(
            0, int(self._value("auto_backup_throttle_minutes")) * 60
        )
        return store

    async def _io(self, function: Any, *args: Any) -> Any:
        """Keep caller-owned pins and locks until an executor operation finishes."""
        return await await_owned_io(self.manager.settings_ui._io(function, *args))

    async def helper_backup(
        self, payload: dict[str, Any], context: CallContext
    ) -> dict | None:
        """The caller holds the helper's write lock and rechecks before commit."""
        if not self.manager.policy.flag("enable_auto_backup") or not payload.get(
            "backup_id"
        ):
            return None
        expected_registry = (
            self._storage_registry(payload)
            if payload.get("kind") not in {"flow", "subentry", "integration"}
            else None
        )
        try:
            current = await self._helper_config(payload)
        except _FlowHelperReadError as error:
            # A partial/scrubbed value is never a usable restore point. Keep
            # the existing best-effort backup contract and expose the reason.
            return {"warnings": [f"No pre-write backup was taken: {error.reason}"]}
        domain = payload.get("backup_domain", f"helper_{payload['helper_type']}")
        source = payload.get("source_tool", "ha_config_set_helper")
        if (domain, source) not in {
            (f"helper_{payload['helper_type']}", "ha_config_set_helper"),
            (f"helper_{payload['helper_type']}", "ha_remove_helpers_integrations"),
            (f"helper_{payload['helper_type']}", "ha_set_integration"),
        } and (domain, source, payload["helper_type"]) not in {
            ("zone", "ha_set_zone", "zone"),
            ("zone", "ha_remove_zone", "zone"),
            ("integration", "ha_remove_helpers_integrations", "integration"),
            ("integration", "ha_set_integration", "integration"),
        }:
            raise ValueError("Unsupported native snapshot domain/source pair")
        result = await self._capture_value(
            domain, str(payload["backup_id"]), current, source
        )
        return (
            {**(result or {}), "expected_registry": expected_registry}
            if expected_registry is not None
            else result
        )

    def _storage_registry(self, payload: dict[str, Any]) -> dict[str, Any]:
        from homeassistant.helpers import entity_registry as er

        entity_id = payload.get("entity_id")
        row = (
            er.async_get(self.manager.hass).async_get(entity_id) if entity_id else None
        )
        values = {"icon": row.icon if row else None}
        if payload["helper_type"] == "tag":
            values["name"] = row.name if row else None
        return {
            "entity_id": entity_id if row else None,
            "id": getattr(row, "id", None),
            "platform": payload["helper_type"],
            "unique_id": str(payload["current_item"]["id"]),
            "values": values,
        }

    async def _helper_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        from .native_operations.snapshot_sources import flow_snapshot, subentry_snapshot

        if payload.get("kind") == "integration":
            current_entry = payload.get("current_entry")
            if (
                not isinstance(current_entry, dict)
                or current_entry.get("entry_id") != payload.get("entry_id")
                or {"data", "options"}.intersection(current_entry)
            ):
                raise _FlowHelperReadError(
                    "Integration snapshot requires public metadata only",
                    "integration_metadata",
                )
            return copy.deepcopy(current_entry)
        if payload.get("kind") == "flow":
            return await flow_snapshot(self.manager.hass, payload)
        if payload.get("kind") == "subentry":
            return await subentry_snapshot(self.manager.hass, payload)
        current: dict[str, Any] = copy.deepcopy(payload["current_item"])
        entity_id = payload.get("entity_id")
        icon = None
        if entity_id:
            from homeassistant.helpers import entity_registry as er

            row = er.async_get(self.manager.hass).async_get(entity_id)
            icon = row.icon if row else None
            if payload.get("helper_type") == "tag" and row is not None:
                current["name"] = row.name
        # The registry is read before the first await, alongside the exact item
        # selected by the backend, never after a new alias/ID lookup.
        current["registry_icon"] = icon
        return current

    async def config_backup(
        self, payload: dict[str, Any], context: CallContext
    ) -> dict[str, Any] | None:
        """Capture the item Core's config owner just resolved, before its compare/write."""
        domain, source = payload["domain"], payload["source_tool"]
        if domain not in {"automation", "script", "scene"} or source not in {
            f"ha_config_set_{domain}",
            f"ha_config_remove_{domain}",
        }:
            raise ValueError("Unsupported native config snapshot domain/source pair")
        if (
            not self.manager.policy.flag("enable_auto_backup")
            or not payload.get("backup_id")
            or payload.get("current_config") is None
        ):
            return None
        return await self._capture_value(
            domain, payload["backup_id"], payload["current_config"], source
        )

    async def registry_backup(
        self, payload: dict[str, Any], context: CallContext
    ) -> dict[str, Any] | None:
        """Capture the registry owner's exact pre-image without selecting it again."""
        domain, source = payload["domain"], payload["source_tool"]
        allowed = {
            "category": {"ha_config_set_category", "ha_config_remove_category"},
            "label": {"ha_config_set_label", "ha_config_remove_label"},
            "area_or_floor": {
                "ha_set_area_or_floor",
                "ha_remove_area_or_floor",
                "ha_config_set_label",
            },
            "device": {"ha_set_device", "ha_remove_device"},
            "entity": {"ha_set_entity", "ha_remove_entity"},
        }
        if source not in allowed.get(domain, set()):
            raise ValueError("Unsupported native registry snapshot domain/source pair")
        if (
            not self.manager.policy.flag("enable_auto_backup")
            or not payload.get("backup_id")
            or payload.get("current_config") is None
        ):
            return None
        if not isinstance(payload["current_config"], dict):
            raise ValueError("Registry snapshot must contain an object")
        return await self._capture_value(
            domain, payload["backup_id"], payload["current_config"], source
        )

    async def todo_backup(
        self, payload: dict[str, Any], context: CallContext
    ) -> dict[str, Any] | None:
        """Capture the exact item resolved by todo.item.prepare, without rereading."""
        if not self.manager.policy.flag("enable_auto_backup") or not payload.get(
            "item"
        ):
            return None
        source = payload.get("source_tool")
        if source not in {"ha_set_todo_item", "ha_remove_todo_item"}:
            raise ValueError("Unsupported native todo snapshot source")
        entity_id, item = payload["entity_id"], payload["item"]
        current = payload.get("current_item")
        if current is None:
            return {"warnings": ["No pre-write backup was taken: unresolved_todo_item"]}
        if not isinstance(current, dict) or item not in (
            current.get("uid"),
            current.get("summary"),
        ):
            raise ValueError("Resolved todo snapshot does not match its target")
        config = {"todo_entity_id": entity_id, **copy.deepcopy(current)}
        return await self._capture_value(
            "todo_item", f"{entity_id}::{item}", config, source
        )

    async def _capture_value(
        self, domain: str, identifier: str, config: Any, source: str
    ) -> dict[str, Any] | None:
        current, store = copy.deepcopy(config), self._store()

        def capture() -> None:
            from homeassistant.helpers.json import json_bytes

            store.capture(domain, identifier, json.loads(json_bytes(current)), source)

        return await self._capture(capture)

    async def snapshot_comparison(
        self, name: str, actor: CallContext
    ) -> tuple[dict[str, Any], Any]:
        from ha_mcp.runtime.snapshot_compare import comparison_values
        from ha_mcp.runtime.snapshot_files import _flow_entry_id, _is_flow_helper_domain
        from ha_mcp.runtime.snapshot_payloads import validate_snapshot_identity

        await self.manager.backend.authorize(actor)
        data = await self._io(self._store().read_snapshot, name)
        validate_snapshot_identity(data)
        if _is_flow_helper_domain(data["domain"]):
            data["entity_id"] = _flow_entry_id(
                data["entity_id"],
                data["config"],
                data["domain"].removeprefix("helper_"),
            )
        current = await self.domains.current(data, actor)
        return comparison_values(data, current)

    async def diff_snapshot(self, name: str, actor: CallContext) -> Any:
        from ha_mcp.runtime.snapshot_compare import diff_snapshot_values

        data, current = await self.snapshot_comparison(name, actor)
        return await self._io(diff_snapshot_values, name, data, current)

    def _require_restore_access(self, actor: CallContext) -> None:
        from ha_mcp.runtime.mutation_guard import check_mutation_admission

        check_mutation_admission(actor)
        self._check_restore_controls(actor)

    def _check_restore_controls(self, actor: CallContext) -> None:
        from ha_mcp.tool_api import ToolFailure

        if (
            actor.read_only
            or self.manager.policy.read_only
            or self.manager.policy.flag("backup_read_only")
        ):
            raise ToolFailure(
                "READ_ONLY", "Backup restoration is read only", status="not_started"
            )

    async def restore_snapshot(
        self, name: str, actor: CallContext, *, take_safety_backup: bool = True
    ) -> dict[str, Any]:
        from .native_snapshot_restore import restore_controls

        await self.manager.backend.authorize(actor)
        self._require_restore_access(actor)
        with restore_controls(self, actor):
            return await self._restore_snapshot(name, actor, take_safety_backup)

    async def _restore_snapshot(
        self, name: str, actor: CallContext, take_safety_backup: bool
    ) -> dict[str, Any]:
        from ha_mcp.errors import HomeAssistantError
        from ha_mcp.runtime.snapshot_files import _is_flow_helper_domain
        from ha_mcp.runtime.snapshot_flow_values import _validate_flow_snapshot
        from ha_mcp.runtime.snapshot_payloads import (
            MandatoryBackupError,
            validate_snapshot_identity,
        )
        from ha_mcp.runtime.snapshot_restore import restore_snapshot_data
        from ha_mcp.tool_api import ToolFailure
        from ha_mcp.utils.registry_update_lock import registry_update_lock

        from .native_snapshot_restore import NativeRestoreOperations

        store = self._store()
        store._protect_snapshot(name)
        try:
            data = await self._io(store.read_snapshot, name)
            validate_snapshot_identity(data)
            if _is_flow_helper_domain(data["domain"]):
                data["entity_id"] = _validate_flow_snapshot(
                    data["entity_id"],
                    data["config"],
                    data["domain"].removeprefix("helper_"),
                )["entry_id"]
            config = data["config"]
            target = (
                str(config.get("id", data["entity_id"]))
                if isinstance(config, dict)
                else data["entity_id"]
            )
            lock_domain = data["domain"]
            if lock_domain == "area_or_floor":
                from ha_mcp.runtime.snapshot_registry_restore import registry_target

                lock_domain, identity, _metadata = registry_target(
                    lock_domain, data["entity_id"], config
                )
                target = identity[f"{lock_domain}_id"]
            async with registry_update_lock(lock_domain, target):
                return await restore_snapshot_data(
                    store,
                    name,
                    data,
                    NativeRestoreOperations(self, data, actor, store),
                    take_safety_backup=take_safety_backup,
                    restore_errors=(
                        OSError,
                        HomeAssistantError,
                        ToolFailure,
                        MandatoryBackupError,
                        PermissionError,
                    ),
                )
        finally:
            store._unprotect_snapshot(name)

    async def _capture(self, capture: Any) -> dict[str, Any] | None:
        future = self.manager.hass.async_add_executor_job(capture)
        self.pending.add(future)

        def completed(done: asyncio.Future[Any]) -> None:
            self.pending.discard(done)
            if not done.cancelled():
                # A disconnected caller can stop awaiting the shielded I/O.
                # Observe late errors while retaining them for normal waiters.
                done.exception()

        future.add_done_callback(completed)
        try:
            await asyncio.shield(future)
        except (OSError, yaml.YAMLError) as error:
            _LOGGER.warning(
                "No pre-write helper backup was taken: %s", type(error).__name__
            )
            return {
                "warnings": [f"No pre-write backup was taken: {type(error).__name__}"]
            }
        return None

    async def async_stop(self) -> None:
        if self.pending:
            _, pending = await asyncio.wait(tuple(self.pending), timeout=10)
            if pending:
                raise RuntimeError("Owned backup I/O did not finish before unload")
