"""Read and mutate the live Lovelace owners, retaining their Core persistence."""

from __future__ import annotations

import copy
import dis
from dataclasses import dataclass, field, replace
from typing import Any

from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation
from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.owned_executor import await_owned_io

from ..helper_collections import _INVALID_ERRORS, collection_owner
from .command_validation import _validated_payload
from .registry_access import (
    Authorize,
    admit_registry_write,
    authorize_actor,
    registry_json,
)
from .storage_api import require_core_api, require_non_suspending


@dataclass(frozen=True, slots=True)
class DashboardState:
    url_path: str
    owner: Any = field(repr=False)
    row: dict[str, Any] | None
    config: dict[str, Any] | None
    verified: bool = True
    warnings: tuple[str, ...] = ()

    @property
    def snapshot(self) -> dict[str, Any] | None:
        if self.owner is None or (self.row is None and self.config is None):
            return None
        return {
            "dashboard": copy.deepcopy(self.row),
            "config": copy.deepcopy(self.config),
        }


def _config_assignment(instructions: tuple[dis.Instruction, ...]) -> int:
    for index, operation in enumerate(instructions):
        if index < 3 or operation.opname != "STORE_SUBSCR":
            continue
        owner, attribute, key = instructions[index - 3 : index]
        loads_self = owner.opname.startswith("LOAD_FAST") and (
            owner.argval == "self"
            or (isinstance(owner.argval, tuple) and owner.argval[-1] == "self")
        )
        if (
            loads_self
            and attribute.opname == "LOAD_ATTR"
            and attribute.argval == "_data"
            and key.opname == "LOAD_CONST"
            and key.argval == "config"
        ):
            return index
    raise UnsupportedOperation("dashboards.changed_cached_save_protocol")


def require_cached_save(owner: Any) -> None:
    """Core commits its loaded cache before yielding to Store persistence."""
    from homeassistant.components.lovelace.dashboard import LovelaceStorage

    if type(owner) is not LovelaceStorage or not isinstance(owner._data, dict):
        raise UnsupportedOperation("dashboards.loaded_storage_owner")
    instructions = tuple(dis.get_instructions(type(owner).async_save))
    commit = _config_assignment(instructions)
    prefix = instructions[:commit]
    awaits = [i for i, op in enumerate(prefix) if op.opname == "GET_AWAITABLE"]
    cached_skip = not awaits or any(
        index > 0
        and operation.opname.endswith("IF_NOT_NONE")
        and prefix[index - 1].argval == "_data"
        and isinstance(operation.argval, int)
        and prefix[awaits[0]].offset < operation.argval <= instructions[commit].offset
        for index, operation in enumerate(prefix[: awaits[0]])
    )
    if (
        len(awaits) > 1
        or not cached_skip
        or (awaits and not any(op.argval == "_load" for op in prefix[: awaits[0]]))
    ):
        raise UnsupportedOperation("dashboards.changed_cached_save_protocol")


@dataclass(slots=True)
class DashboardStorage:
    hass: Any
    actor: CallContext
    authorize: Authorize = field(kw_only=True, repr=False)
    started: bool = field(default=False, init=False)
    completed: bool = field(default=False, init=False)
    config_saved: bool = field(default=False, init=False)

    def _dashboards(self) -> dict[Any, Any]:
        data = self.hass.data.get("lovelace")
        dashboards = getattr(data, "dashboards", None)
        if not isinstance(dashboards, dict):
            raise ToolFailure(
                "RESOURCE_NOT_FOUND",
                "Lovelace dashboards are unavailable",
                status="not_started",
            )
        return dashboards

    def resolve(self, identifier: str | None) -> tuple[str, Any]:
        dashboards = self._dashboards()
        name = "lovelace" if identifier in (None, "default", "lovelace") else identifier
        owner = dashboards.get(name)
        if owner is None and name == "lovelace":
            owner = dashboards.get(None)
        if owner is None:
            owner = next(
                (
                    dashboard
                    for dashboard in dashboards.values()
                    if isinstance(dashboard.config, dict)
                    and dashboard.config.get("id") == name
                ),
                None,
            )
            if owner is not None:
                name = owner.url_path or "lovelace"
        return name, owner

    @staticmethod
    def _row(owner: Any) -> dict[str, Any] | None:
        return (
            registry_json(owner.config)
            if owner is not None
            and isinstance(owner.config, dict)
            and owner.mode == "storage"
            else None
        )

    def metadata(self) -> list[dict[str, Any]]:
        return [
            {**registry_json(owner.config), "url_path": key, "mode": owner.mode}
            for key, owner in self._dashboards().items()
            if key is not None and isinstance(owner.config, dict)
        ]

    def _storage(self, owner: Any) -> None:
        from homeassistant.components.lovelace.dashboard import LovelaceStorage

        if owner is None:
            raise ToolFailure(
                "RESOURCE_NOT_FOUND", "Dashboard not found", status="not_started"
            )
        if type(owner) is not LovelaceStorage:
            raise ToolFailure(
                "VALIDATION_FAILED",
                "Only storage-mode dashboards can be changed; edit a YAML dashboard in its YAML file.",
                status="not_started",
            )
        if self.hass.config.recovery_mode:
            raise ToolFailure(
                "SERVICE_CALL_FAILED",
                "Dashboard writes are unavailable in recovery mode",
                status="not_started",
            )

    def _collection(self) -> Any:
        from homeassistant.components.lovelace.dashboard import DashboardsCollection

        owner = collection_owner(self.hass, "lovelace/dashboards")
        collection = getattr(owner, "storage_collection", None)
        if type(collection) is not DashboardsCollection:
            raise UnsupportedOperation("dashboards.collection_owner")
        return collection

    async def read(
        self, identifier: str | None, *, force: bool = False
    ) -> DashboardState:
        from homeassistant.components.lovelace.const import ConfigNotFound

        await authorize_actor(self.actor, self.authorize)
        url_path, owner = self.resolve(identifier)
        row = self._row(owner)
        if owner is None:
            return DashboardState(url_path, None, None, None)
        try:
            config = await await_owned_io(owner.async_load(force))
        except ConfigNotFound:
            config = None
        await authorize_actor(self.actor, self.authorize)
        if self.resolve(url_path)[1] is not owner or not json_values_equal(
            self._row(owner), row
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Dashboard changed during its read",
                status="not_started",
            )
        return DashboardState(
            url_path, owner, row, registry_json(config) if config is not None else None
        )

    def check(self, before: DashboardState) -> None:
        current = self.resolve(before.url_path)[1]
        if current is not before.owner or not json_values_equal(
            self._row(current), before.row
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Dashboard ownership or metadata changed after its before-image",
                status="not_started",
            )
        if current is None:
            return
        self._storage(current)
        require_cached_save(current)
        if not json_values_equal(
            registry_json(current._data["config"])
            if current._data["config"] is not None
            else None,
            before.config,
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Dashboard config changed after its before-image",
                status="not_started",
            )

    def failure(self, error: Exception) -> ToolFailure:
        if self.started:
            return ToolFailure(
                "OUTCOME_UNKNOWN",
                "A dashboard write started; inspect its current state before retrying",
                data={
                    "success": False,
                    "error": {
                        "code": "OUTCOME_UNKNOWN",
                        "message": "Dashboard operation did not complete normally",
                    },
                    "write_committed": True if self.completed else None,
                    "config_applied": True if self.config_saved else None,
                    "post_write_verified": False,
                },
                status="outcome_unknown",
            )
        if isinstance(error, ToolFailure):
            if error.status is None:
                error.status = "not_started"
            return error
        return ToolFailure(
            "SERVICE_CALL_FAILED",
            f"Dashboard operation failed ({type(error).__name__})",
            status="not_started",
        )

    async def save(
        self, before: DashboardState, candidate: dict[str, Any]
    ) -> DashboardState:
        # Core receives a detached value, not the snapshot or caller's mutable object.
        values = registry_json(candidate)
        await await_owned_io(self._save(before, values))
        try:
            current = await self.read(before.url_path)
            if current.owner is not before.owner:
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "Dashboard owner changed during save verification",
                    status="not_started",
                )
            if not json_values_equal(current.config, values):
                return replace(
                    before,
                    config=values,
                    verified=False,
                    warnings=(
                        "Dashboard save completed, but its current config differs from the saved candidate. Another writer may have changed it; read the dashboard before editing again.",
                    ),
                )
            return current
        except ToolFailure as error:
            if error.code == "AUTH_FORBIDDEN":
                raise
        except Exception:  # noqa: BLE001 - preserve a completed write when Core readback fails
            pass
        await authorize_actor(self.actor, self.authorize)
        return replace(
            before,
            config=values,
            verified=False,
            warnings=(
                "Dashboard save completed, but its current config could not be verified. Read the dashboard before editing it again.",
            ),
        )

    async def _save(self, before: DashboardState, values: dict[str, Any]) -> None:
        await admit_registry_write(self.actor, self.authorize)
        self.check(before)
        self._storage(before.owner)
        check_mutation_admission(self.actor)
        self.started = True
        await before.owner.async_save(values)
        self.completed = True
        self.config_saved = True

    async def clear(self, before: DashboardState) -> DashboardState:
        await await_owned_io(self._clear(before))
        try:
            current = await self.read(before.url_path)
            if current.owner is before.owner and current.config is None:
                return current
        except ToolFailure as error:
            if error.code == "AUTH_FORBIDDEN":
                raise
        except Exception:  # noqa: BLE001 - the clear completed, but readback may be unavailable
            pass
        await authorize_actor(self.actor, self.authorize)
        return replace(
            before,
            config=None,
            verified=False,
            warnings=(
                "Dashboard reset completed, but the same owner could not be verified without a saved config. Another writer may have changed it; inspect the dashboard before retrying.",
            ),
        )

    async def _clear(self, before: DashboardState) -> None:
        await admit_registry_write(self.actor, self.authorize)
        self.check(before)
        self._storage(before.owner)
        check_mutation_admission(self.actor)
        self.started = True
        await before.owner.async_delete()
        self.completed = True
        self.config_saved = True

    async def create(
        self, before: DashboardState, values: dict[str, Any]
    ) -> DashboardState:
        fields = _validated_payload(
            self.hass,
            "lovelace/dashboards/create",
            {"url_path": before.url_path, **values},
        )
        await await_owned_io(self._metadata_write(before, "create", fields))
        created = await self.read(before.url_path)
        if created.config is not None:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The new dashboard acquired a config before this operation could initialize it",
                status="not_started",
            )
        return created

    async def update_metadata(
        self, before: DashboardState, values: dict[str, Any]
    ) -> DashboardState:
        if before.row is None:
            raise ToolFailure(
                "SERVICE_CALL_FAILED",
                "Dashboard has no storage ID for metadata changes",
                status="not_started",
            )
        fields = _validated_payload(
            self.hass,
            "lovelace/dashboards/update",
            {"dashboard_id": before.row["id"], **values},
        )
        fields.pop("dashboard_id")
        await await_owned_io(self._metadata_write(before, "update", fields))
        current = await self.read(before.url_path)
        if current.owner is not before.owner or not json_values_equal(
            current.config, before.config
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Dashboard config changed while metadata was being saved; no config replacement was sent",
                status="not_started",
            )
        return current

    async def _metadata_write(
        self, before: DashboardState, action: str, fields: dict[str, Any]
    ) -> None:
        collection = self._collection()
        require_core_api(collection, action)
        validator = (
            collection._process_create_data
            if action == "create"
            else collection._update_data
        )
        require_non_suspending(validator)
        try:
            if action == "create":
                await validator(copy.deepcopy(fields))
            else:
                await validator(copy.deepcopy(before.row), copy.deepcopy(fields))
        except _INVALID_ERRORS as error:
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER", str(error), status="not_started"
            ) from error
        await admit_registry_write(self.actor, self.authorize)
        self.check(before)
        if self._collection() is not collection:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Dashboard collection changed before its write",
                status="not_started",
            )
        self._check_collection_row(collection, before)
        check_mutation_admission(self.actor)
        self.started = True
        if action == "create":
            await collection.async_create_item(fields)
        else:
            assert before.row is not None
            await collection.async_update_item(before.row["id"], fields)
        self.completed = True

    @staticmethod
    def _check_collection_row(collection: Any, before: DashboardState) -> None:
        current = (
            collection.data.get(before.row["id"])
            if before.row
            else next(
                (
                    row
                    for row in collection.data.values()
                    if row.get("url_path") == before.url_path
                ),
                None,
            )
        )
        if not json_values_equal(
            registry_json(current) if current is not None else None, before.row
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Dashboard collection changed before its owner notification completed",
                status="not_started",
            )

    async def remove(self, before: DashboardState) -> None:
        if before.row is None:
            raise ToolFailure(
                "RESOURCE_NOT_FOUND",
                "No storage dashboard can be deleted at this path",
                status="not_started",
            )
        _validated_payload(
            self.hass, "lovelace/dashboards/delete", {"dashboard_id": before.row["id"]}
        )
        await await_owned_io(self._remove(before))

    async def _remove(self, before: DashboardState) -> None:
        from homeassistant.helpers.collection import StorageCollection

        collection = self._collection()
        if (
            type(collection).async_delete_item
            is not StorageCollection.async_delete_item
        ):
            raise UnsupportedOperation("dashboards.collection_delete_owner")
        await admit_registry_write(self.actor, self.authorize)
        self.check(before)
        if self._collection() is not collection:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Dashboard collection changed before delete",
                status="not_started",
            )
        self._check_collection_row(collection, before)
        check_mutation_admission(self.actor)
        self.started = True
        assert before.row is not None
        await collection.async_delete_item(before.row["id"])
        self.completed = True
