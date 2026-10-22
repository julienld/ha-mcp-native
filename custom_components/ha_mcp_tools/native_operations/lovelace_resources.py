"""Dashboard resources use Core's own loaded collection and mutation methods."""

from __future__ import annotations

import copy
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.helpers.json import json_bytes

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.runtime.resource_values import (
    _YAML_MODE_MESSAGE,
    _data_uri_for,
    _decode_inline_content,
    _is_preview_of,
    _looks_like_preview,
    _require_created_resource_id,
    resource_list_result,
)
from ha_mcp.runtime.resource_write_values import (
    resource_delete_result,
    resource_set_result,
    validate_choice,
    validate_inline,
)
from ha_mcp.runtime.validation import validate_identifier_not_empty
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation
from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.owned_executor import await_owned_io
from ha_mcp.utils.registry_update_lock import registry_update_lock

from ..helper_collections import _INVALID_ERRORS
from .storage_api import require_non_suspending

_CURRENT = object()
type ResourceChange = tuple[dict[str, Any] | None, list[str]]


def resources_need_loading(hass: Any) -> bool:
    """Core's LOVELACE_DATA is the integration's 'lovelace' HassKey."""
    resources = getattr(hass.data.get("lovelace"), "resources", None)
    return getattr(resources, "loaded", False) is not True


@dataclass(slots=True)
class NativeResources:
    hass: Any
    actor: CallContext
    authorize: Callable[[CallContext], Awaitable[None]] = field(
        kw_only=True, repr=False
    )
    backup: (
        Callable[[dict[str, Any], CallContext], Awaitable[dict[str, Any] | None]] | None
    ) = field(default=None, kw_only=True, repr=False)
    _load_started: bool = field(default=False, init=False)
    _write_started: bool = field(default=False, init=False)
    _write_completed: bool = field(default=False, init=False)

    async def _authorize(self) -> None:
        try:
            await self.authorize(self.actor)
        except PermissionError as error:
            raise ToolFailure(
                "AUTH_FORBIDDEN",
                "Administrator admission was revoked",
                status="not_started",
            ) from error

    async def _admit(self) -> None:
        await self._authorize()
        check_mutation_admission(self.actor)
        if self.actor.read_only:
            raise ToolFailure(
                "READ_ONLY", "Resource changes are disabled", status="not_started"
            )

    def _owner(self) -> tuple[Any, str]:
        data = self.hass.data.get("lovelace")
        owner = getattr(data, "resources", None)
        if owner is None:
            raise ToolFailure(
                "RESOURCE_NOT_FOUND",
                "Dashboard resources are unavailable",
                status="not_started",
            )
        return owner, data.resource_mode

    def _failure(self, error: Exception) -> ToolFailure:
        if self._load_started or self._write_started:
            return ToolFailure(
                "OUTCOME_UNKNOWN",
                "A resource operation started; inspect its current state before retrying",
                data={
                    "success": False,
                    "error": {
                        "code": "OUTCOME_UNKNOWN",
                        "message": "Resource initialization or mutation did not complete normally",
                    },
                    "resource_initialization_started": self._load_started,
                    "write_started": self._write_started,
                    "config_applied": True if self._write_completed else None,
                },
                status="outcome_unknown",
            )
        if isinstance(error, ToolFailure):
            if error.status is None:
                error.status = "not_started"
            return error
        return ToolFailure(
            "RESOURCE_OPERATION_FAILED",
            f"Resource operation failed ({type(error).__name__})",
            status="not_started",
        )

    async def _loaded(self) -> tuple[Any, str]:
        async with registry_update_lock("lovelace_resources", "load"):
            await self._authorize()
            owner, mode = self._owner()
            if not owner.loaded:
                await await_owned_io(self._initialize(owner))
                await self._authorize()
            if self._owner()[0] is not owner or not owner.loaded:
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "Resource ownership changed during initialization",
                    status="not_started",
                )
            return owner, mode

    async def _initialize(self, owner: Any) -> None:
        # Admission belongs inside this owned task, after its scheduling boundary.
        await self._admit()
        if self._owner()[0] is not owner:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Resource ownership changed before initialization",
                status="not_started",
            )
        self._load_started = True
        await owner.async_get_info()

    @staticmethod
    def _plain(value: Any) -> Any:
        return json.loads(json_bytes(value))

    def _row(self, owner: Any, identifier: str | None) -> dict[str, Any] | None:
        if identifier is None:
            return None
        row = next(
            (row for row in owner.async_items() if str(row.get("id")) == identifier),
            None,
        )
        return self._plain(row) if row is not None else None

    def _storage(self, owner: Any) -> None:
        from homeassistant.components.lovelace.resources import (
            ResourceStorageCollection,
        )

        if self._owner()[1] == "yaml":
            raise ToolFailure(
                "SERVICE_CALL_FAILED",
                _YAML_MODE_MESSAGE,
                data=create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    _YAML_MODE_MESSAGE,
                    context={"resource_mode": "yaml"},
                ),
                status="not_started",
            )
        if type(owner) is not ResourceStorageCollection:
            raise UnsupportedOperation("resources.collection_owner")

    async def list(
        self, include_content: bool = False, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        try:
            owner, mode = await self._loaded()
            # Resource values are strings. Detach rows before running the
            # whole-set decode/count projection in HA's executor.
            rows = [dict(row) for row in owner.async_items()]
            result: dict[str, Any] = await await_owned_io(
                self.hass.async_add_executor_job(
                    resource_list_result, rows, include_content, limit, offset, mode
                )
            )
            return result
        except Exception as error:
            raise self._failure(error) from error

    async def save(
        self,
        content: str | None = None,
        url: str | None = None,
        resource_type: str = "module",
        resource_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            validate_choice(content, url)
            size = (
                validate_inline(content, resource_type) if content is not None else None
            )
            if resource_id is not None:
                validate_identifier_not_empty(
                    resource_id, "resource_id", context={"action": "set"}
                )
            resource_url = (
                _data_uri_for(content, resource_type) if content is not None else url
            )
            action = "create" if resource_id is None else "update"
            row, warnings = await self._change(
                action,
                resource_id,
                {"url": resource_url, "res_type": resource_type},
                content=content,
            )
            assert row is not None
            identifier = row.get("id", resource_id)
            _require_created_resource_id(
                identifier, "created" if action == "create" else "updated"
            )
            result = resource_set_result(
                identifier,
                resource_type,
                "created" if action == "create" else "updated",
                size=size,
                url=url,
            )
            if warnings:
                result["warnings"] = warnings
            return result
        except Exception as error:
            raise self._failure(error) from error

    async def delete(self, resource_id: str) -> dict[str, Any]:
        try:
            validate_identifier_not_empty(
                resource_id, "resource_id", context={"action": "delete"}
            )
            _, warnings = await self._change("delete", resource_id, {})
            result = resource_delete_result(resource_id)
            if warnings:
                result["warnings"] = warnings
            return result
        except Exception as error:
            raise self._failure(error) from error

    async def _change(
        self,
        action: str,
        identifier: str | None,
        fields: dict[str, Any],
        *,
        content: str | None = None,
        expected: Any = _CURRENT,
        capture: bool = True,
    ) -> ResourceChange:
        await self._authorize()
        initial, _mode = self._owner()
        self._storage(initial)
        fields = self._validated_fields(initial, action, fields)
        owner, _mode = await self._loaded()
        async with registry_update_lock("lovelace_resources", "write"):
            await self._admit()
            self._storage(owner)
            if self._owner()[0] is not owner:
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "Resource ownership changed before mutation",
                    status="not_started",
                )
            before = self._row(owner, identifier)
            if identifier is not None and before is None:
                raise ToolFailure(
                    "RESOURCE_NOT_FOUND",
                    f"Dashboard resource '{identifier}' not found",
                    status="not_started",
                )
            if expected is not _CURRENT and not json_values_equal(before, expected):
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "The resource changed after its safety snapshot",
                    status="not_started",
                )
            fields = self._validated_fields(owner, action, fields)
            self._check_preview(content, before)
            warnings: list[str] = []
            if capture and before is not None and self.backup is not None:
                result = await self.backup(
                    {
                        "resource_id": identifier,
                        "current_resource": copy.deepcopy(before),
                        "source_tool": "ha_config_delete_dashboard_resource"
                        if action == "delete"
                        else "ha_config_set_dashboard_resource",
                    },
                    self.actor,
                )
                warnings = list(result.get("warnings") or []) if result else []
            written = await await_owned_io(
                self._commit(owner, before, action, identifier, fields)
            )
            return written, warnings

    def _validated_fields(
        self, owner: Any, action: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        if action == "delete":
            return fields
        validator = (
            owner._process_create_data if action == "create" else owner._update_data
        )
        require_non_suspending(validator)
        try:
            result: dict[str, Any] = (
                owner.CREATE_SCHEMA if action == "create" else owner.UPDATE_SCHEMA
            )(copy.deepcopy(fields))
            return result
        except _INVALID_ERRORS as error:
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER", str(error), status="not_started"
            ) from error

    @staticmethod
    def _check_preview(content: str | None, before: dict[str, Any] | None) -> None:
        if content is not None and before is not None and _looks_like_preview(content):
            decoded = _decode_inline_content(before.get("url", ""))
            if decoded and _is_preview_of(content, decoded[0]):
                raise ToolFailure(
                    "VALIDATION_INVALID_PARAMETER",
                    "Content is the truncated preview, not the full resource code; saving it would destroy the resource",
                    status="not_started",
                )

    async def _commit(
        self,
        owner: Any,
        before: dict[str, Any] | None,
        action: str,
        identifier: str | None,
        fields: dict[str, Any],
    ) -> dict[str, Any] | None:
        await self._admit()
        if (
            self._owner()[0] is not owner
            or not owner.loaded
            or not json_values_equal(self._row(owner, identifier), before)
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The resource changed after its snapshot; no mutation was sent",
                status="not_started",
            )
        self._write_started = True
        if action == "create":
            written = await owner.async_create_item(fields)
        elif action == "update":
            written = await owner.async_update_item(identifier, fields)
        else:
            await owner.async_delete_item(identifier)
            written = None
        self._write_completed = True
        result: dict[str, Any] | None = self._plain(written)
        return result
