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

from ha_mcp.runtime.snapshot_store import SnapshotStore
from ha_mcp.tool_api import CallContext, UnsupportedOperation

from .const import SERVER_CONFIG_SUBDIR
from .native_server_policy import environment_name

_LOGGER = logging.getLogger(__name__)


class NativeSnapshots:
    def __init__(self, manager: Any) -> None:
        self.manager = manager
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

    async def helper_backup(
        self, payload: dict[str, Any], context: CallContext
    ) -> dict | None:
        """The caller holds the helper's write lock and rechecks before commit."""
        if not self.manager.policy.flag("enable_auto_backup") or not payload.get(
            "backup_id"
        ):
            return None
        if payload.get("kind") == "flow":
            raise UnsupportedOperation("backups.helper.flow.capture")
        current = copy.deepcopy(payload["current_item"])
        entity_id = payload.get("entity_id")
        icon = None
        if entity_id:
            from homeassistant.helpers import entity_registry as er

            row = er.async_get(self.manager.hass).async_get(entity_id)
            icon = row.icon if row else None
        # The registry is read before the first await, alongside the exact item
        # selected by the backend, never after a new alias/ID lookup.
        current["registry_icon"] = icon
        store = self._store()

        def capture() -> None:
            from homeassistant.helpers.json import json_bytes

            normalized = json.loads(json_bytes(current))
            store.capture(
                f"helper_{payload['helper_type']}",
                str(payload["backup_id"]),
                normalized,
                "ha_config_set_helper",
            )

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
