"""Canonical label ownership on Core's loop, without a transport-shaped backend."""

import asyncio
import copy
import importlib
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.runtime.errors import raise_failure
from ha_mcp.runtime.label_values import (
    area_label_addition,
    label_get_result,
    label_remove_result,
    label_set_result,
    parse_areas,
    require_area_ids,
    string_labels,
    unique_area_ids,
    with_retry_suggestions,
)
from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.runtime.snapshot_payloads import InvalidBackupSnapshotError, _strip_readonly
from ha_mcp.runtime.validation import validate_identifier_not_empty
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation
from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .command_validation import _validated_payload
from .label_validation import existing_label_ids
from .registry_access import (
    Authorize,
    Capture,
    admit_registry_write,
    authorize_actor,
    capture_registry_before,
    registry_failure,
    registry_json,
)
from .registry_failures import (
    registry_outcome_unknown,
    registry_target_missing,
    with_recovery_hint,
)

_CURRENT = object()


@dataclass(slots=True)
class NativeLabels:
    hass: HomeAssistant
    actor: CallContext
    authorize: Authorize = field(kw_only=True, repr=False)
    backup: Capture | None = field(default=None, kw_only=True, repr=False)

    async def _authorize(self) -> None:
        await authorize_actor(self.actor, self.authorize)

    async def _admit(self) -> None:
        await admit_registry_write(self.actor, self.authorize)

    def _label_owner(self) -> tuple[Any, Any]:
        from homeassistant.helpers import label_registry as lr

        module = importlib.import_module(
            "homeassistant.components.config.label_registry"
        )
        serialize = getattr(module, "_entry_dict", None)
        if not callable(serialize):
            raise UnsupportedOperation("labels.serializer")
        return lr.async_get(self.hass), serialize

    def _area_owner(self) -> Any:
        from homeassistant.helpers import area_registry as ar

        return ar.async_get(self.hass)

    _json = staticmethod(registry_json)

    def _label_row(
        self, registry: Any, serialize: Any, identifier: str
    ) -> dict[str, Any] | None:
        entry = registry.async_get_label(identifier)
        return self._json(serialize(entry)) if entry is not None else None

    def _area_row(self, registry: Any, identifier: str) -> dict[str, Any] | None:
        entry = registry.async_get_area(identifier)
        return self._json(entry.json_fragment) if entry is not None else None

    _failure = staticmethod(registry_failure)

    async def _capture(
        self,
        domain: str,
        identifier: str | None,
        before: Any,
        source: str,
    ) -> list[str]:
        return await capture_registry_before(
            self.backup,
            self.actor,
            domain,
            identifier,
            before,
            source,
        )

    async def get(self, label_id: str | None = None) -> dict[str, Any]:
        try:
            await self._authorize()
            if label_id is not None:
                validate_identifier_not_empty(
                    label_id,
                    "label_id",
                    suggestions=[
                        "Omit label_id entirely to list all labels",
                        "Pass a valid non-empty label_id",
                    ],
                    context={"action": "get"},
                )
            registry, serialize = self._label_owner()
            return label_get_result(
                [
                    self._json(serialize(entry))
                    for entry in registry.async_list_labels()
                ],
                label_id,
            )
        except Exception as error:
            raise self._failure(error, {"label_id": label_id}) from error

    async def set(
        self,
        name: str,
        label_id: str | None = None,
        color: str | None = None,
        icon: str | None = None,
        description: str | None = None,
        areas: str | list[str] | None = None,
    ) -> dict[str, Any]:
        try:
            await self._admit()
            if label_id is not None:
                validate_identifier_not_empty(
                    label_id,
                    "label_id",
                    suggestions=[
                        "Omit label_id entirely to create a new label",
                        "Pass a valid label_id to update an existing label",
                    ],
                    context={"action": "set", "name": name},
                )
            parsed = parse_areas(areas)
            targets = unique_area_ids(parsed) if parsed else parsed
            if targets:
                require_area_ids(
                    targets,
                    {entry.id for entry in self._area_owner().async_list_areas()},
                )
            action = "create" if label_id is None else "update"
            data: dict[str, Any] = {"name": name}
            if label_id is not None:
                data["label_id"] = label_id
            data.update(
                {
                    key: value
                    for key, value in (
                        ("color", color),
                        ("icon", icon),
                        ("description", description),
                    )
                    if value is not None
                }
            )
            row, warnings, registry = await self._write_label(action, data)
            assert row is not None
            identifier = row["label_id"]
            assigned = await self._assign(
                identifier, row.get("created_at"), registry, targets, warnings
            )
            return label_set_result(name, action, row, label_id, assigned, warnings)
        except Exception as error:
            raise self._failure(error, {"name": name, "label_id": label_id}) from error

    async def remove(self, label_id: str) -> dict[str, Any]:
        try:
            await self._admit()
            validate_identifier_not_empty(
                label_id,
                "label_id",
                suggestions=[
                    "Pass a valid label_id (use ha_config_get_label() to list)"
                ],
                context={"action": "remove"},
            )
            _, warnings, _ = await self._write_label("delete", {"label_id": label_id})
            result = label_remove_result(label_id)
            if warnings:
                result["warnings"] = warnings
            return result
        except Exception as error:
            raise self._failure(error, {"label_id": label_id}) from error

    async def read_snapshot(self, label_id: str) -> dict[str, Any] | None:
        await self._authorize()
        registry, serialize = self._label_owner()
        return self._label_row(registry, serialize, label_id)

    async def restore_snapshot(
        self, label_id: str, config: dict[str, Any], expected: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Restore an existing label; the caller owns the single safety capture."""
        await self._admit()
        if config.get("label_id") != label_id:
            raise InvalidBackupSnapshotError(
                "Label snapshot does not match its identity"
            )
        row, _, _ = await self._write_label(
            "update",
            {**_strip_readonly(config, "label_id"), "label_id": label_id},
            expected=expected,
            capture=False,
        )
        assert row is not None
        return row

    async def _write_label(
        self,
        action: str,
        data: dict[str, Any],
        *,
        expected: Any = _CURRENT,
        capture: bool = True,
    ) -> tuple[dict[str, Any] | None, list[str], Any]:
        identifier = data.get("label_id")
        async with (
            registry_update_lock("label", identifier)
            if identifier is not None
            else nullcontext()
        ):
            registry, serialize = self._label_owner()
            payload = _validated_payload(
                self.hass, f"config/label_registry/{action}", data
            )
            before = (
                self._label_row(registry, serialize, identifier)
                if identifier is not None
                else None
            )
            if identifier is not None and before is None:
                raise with_recovery_hint(
                    ToolFailure(
                        "RESOURCE_NOT_FOUND",
                        f"Label not found: {identifier}",
                        status="not_started",
                    ),
                    "To create a new label, omit label_id; use ha_config_get_label() to find an existing label"
                    if action == "update"
                    else "Use ha_config_get_label() to find an existing label before removing it",
                )
            if expected is not _CURRENT and not json_values_equal(before, expected):
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "The label changed after the restore safety capture; no mutation was sent.",
                    status="not_started",
                )
            source = (
                "ha_config_remove_label"
                if action == "delete"
                else "ha_config_set_label"
            )
            warnings = (
                await self._capture("label", identifier, before, source)
                if capture
                else []
            )
            await self._admit()
            current_registry, current_serializer = self._label_owner()
            current = (
                self._label_row(current_registry, current_serializer, identifier)
                if identifier is not None
                else None
            )
            if current_registry is not registry or not json_values_equal(
                current, before
            ):
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "The label changed after its snapshot was read; no mutation was sent.",
                    status="not_started",
                )
            check_mutation_admission(self.actor)
            try:
                if action == "create":
                    written = registry.async_create(**payload)
                elif action == "update":
                    written = registry.async_update(**payload)
                else:
                    registry.async_delete(**payload)
                    return None, warnings, registry
                row = self._json(serialize(written))
                if (
                    not isinstance(row.get("label_id"), str)
                    or not row["label_id"]
                    or (identifier is not None and row["label_id"] != identifier)
                ):
                    raise ValueError("The Core label result has no matching identity")
                return row, warnings, registry
            except Exception as error:  # noqa: BLE001 - save/listener/serializer can fail after mutation
                return registry_outcome_unknown("label", action, error)

    @staticmethod
    def _partial(
        error: Exception, label_id: str, area_id: str, assigned: list[str]
    ) -> ToolFailure:
        failure = NativeLabels._failure(
            error, {"label_id": label_id, "area_id": area_id}
        )
        payload = copy.deepcopy(failure.data) if isinstance(failure.data, dict) else {}
        if not isinstance(payload.get("error"), dict):
            payload["error"] = {"code": failure.code, "message": str(failure)}
        payload.update(
            {
                "partial": True,
                "label_applied": True,
                "label_id": label_id,
                "area_id": area_id,
                "assigned_areas": list(assigned),
            }
        )
        payload["error"] = with_retry_suggestions(payload)
        status = (
            "outcome_unknown" if failure.status == "outcome_unknown" else "confirmed"
        )
        return ToolFailure(failure.code, str(failure), data=payload, status=status)

    async def _assign(
        self,
        label_id: str,
        created_at: Any,
        label_registry: Any,
        targets: list[str] | None,
        warnings: list[str],
    ) -> list[str] | None:
        if targets is None:
            return None
        assigned: list[str] = []
        for index, area_id in enumerate(targets):
            if index:
                await asyncio.sleep(0)
            try:
                await self._assign_one(
                    label_id, created_at, label_registry, area_id, warnings
                )
            except Exception as error:
                raise self._partial(error, label_id, area_id, assigned) from error
            assigned.append(area_id)
        return assigned

    def _check_label_identity(
        self, label_id: str, created_at: Any, expected_owner: Any
    ) -> None:
        owner, serialize = self._label_owner()
        current = self._label_row(owner, serialize, label_id)
        if (
            created_at is None
            or owner is not expected_owner
            or current is None
            or not json_values_equal(current.get("created_at"), created_at)
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The label's identity changed before area assignment; nothing was added to this area.",
                status="not_started",
            )

    async def _assign_one(
        self,
        label_id: str,
        created_at: Any,
        label_registry: Any,
        area_id: str,
        warnings: list[str],
    ) -> None:
        async with registry_update_lock("area", area_id):
            await self._admit()
            registry = self._area_owner()
            before = self._area_row(registry, area_id)
            if before is None:
                registry_target_missing("area", area_id)
            self._check_label_identity(label_id, created_at, label_registry)
            merged = area_label_addition(before, label_id)
            if merged is None:
                return
            payload = _validated_payload(
                self.hass,
                "config/area_registry/update",
                {"area_id": area_id, "labels": merged},
            )
            warnings.extend(
                await self._capture(
                    "area_or_floor",
                    f"area:{area_id}",
                    {**before, "kind": "area"},
                    "ha_config_set_label",
                )
            )
            await self._admit()
            current_owner = self._area_owner()
            if current_owner is not registry or not json_values_equal(
                self._area_row(current_owner, area_id), before
            ):
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "The area changed after its snapshot was read; no assignment was sent.",
                    status="not_started",
                )
            self._check_label_identity(label_id, created_at, label_registry)
            labels = set(payload["labels"])
            payload["labels"] = existing_label_ids(self.hass, labels)
            check_mutation_admission(self.actor)
            try:
                registry.async_update(**payload)
                actual = self._area_row(registry, area_id)
            except Exception as error:  # noqa: BLE001 - callback/save/listener may already have changed the area
                registry_outcome_unknown("area", "update", error)
            if label_id not in string_labels(actual):
                raise_failure(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Area {area_id!r} update succeeded but label {label_id!r} is not present on the area.",
                        context={"write_committed": True},
                        suggestions=[
                            "The label write already succeeded; re-read the area before retrying."
                        ],
                    )
                )
