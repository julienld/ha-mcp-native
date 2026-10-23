"""Compound entity writes capture real owners before a short Core commit sequence."""

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.runtime.entity_fields import (
    build_name_visibility_fields,
    build_state_tag_fields,
    reject_name_with_use_device_name,
    reject_use_device_name_without_device,
)
from ha_mcp.runtime.entity_snapshot_values import entity_registry_identity
from ha_mcp.runtime.entity_write_values import (
    apply_entity_rename,
    entity_write_result,
    merge_entity_aliases,
    merge_entity_labels,
    parse_entity_write,
)
from ha_mcp.runtime.errors import raise_failure
from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.runtime.validation import clearable_value
from ha_mcp.tool_api import ToolFailure
from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .command_validation import _validated_payload
from .device_updates import commit_device_update, prepare_device_update
from .devices import NativeDevices
from .entity_registry import NativeEntityRegistry
from .legacy_exposure import NativeLegacyExposure
from .registry_access import (
    admit_registry_write,
    capture_registry_before,
    registry_failure,
    registry_json,
)
from .registry_failures import registry_outcome_unknown

_CLEAR_DEVICE = "new_device_name cannot clear the device name; use ha_set_device(name='') instead, or name=' ' if your client cannot send an empty string"


@dataclass(slots=True)
class _EntityPlan:
    registry: Any
    before: dict[str, Any]
    payloads: list[dict[str, Any]]
    exposure: list[dict[str, Any]]
    references: dict[str, Any]
    updates: list[str]
    fields: dict[str, Any]
    device: tuple[Any, Any, dict[str, Any], dict[str, Any]] | None = None
    device_result: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    confirmed: list[str] = field(default_factory=list)
    options_succeeded: dict[str, Any] = field(default_factory=dict)
    exposure_succeeded: dict[str, bool] = field(default_factory=dict)


class NativeEntityWrites(NativeEntityRegistry):
    async def set(self, entity_id: str | list[str], **changes: Any) -> dict[str, Any]:
        try:
            await admit_registry_write(self.actor, self.authorize)
            targets, bulk, fields = parse_entity_write(entity_id, changes)
            if not bulk:
                return await self._single(targets[0], fields)
            self._references(
                {
                    "labels": fields["labels"]
                    if fields["label_operation"] in {"set", "add"}
                    else None,
                    "categories": fields["categories"] or {},
                }
            )
            return await self._many(targets, fields)
        except Exception as error:
            raise registry_failure(error, {"entity_id": entity_id}) from error

    async def _many(self, targets: list[str], fields: dict[str, Any]) -> dict[str, Any]:
        succeeded, failed, not_attempted = [], [], []
        unknown = False
        has_confirmed_changes = False
        for index, target in enumerate(targets):
            if index:
                await asyncio.sleep(0)
            try:
                row = await self._single(target, fields)
                succeeded.append(
                    {
                        "entity_id": row["entity_id"],
                        "entity_entry": row.get("entity_entry"),
                        "updates": row.get("updates", []),
                        **(
                            {"warnings": row["warnings"]} if row.get("warnings") else {}
                        ),
                    }
                )
            except ToolFailure as error:
                failed.append(
                    {
                        "entity_id": target,
                        "error": str(error),
                        "code": error.code,
                        "status": error.status,
                        "details": error.data,
                    }
                )
                has_confirmed_changes |= error.status == "confirmed" or bool(
                    error.data.get("confirmed_phases")
                    or error.data.get("exposure_succeeded")
                )
                if error.status == "outcome_unknown":
                    unknown = True
                    not_attempted = targets[index + 1 :]
                    break
        result = {
            "success": not failed,
            "total": len(targets),
            "succeeded_count": len(succeeded),
            "failed_count": len(failed),
            "succeeded": succeeded,
        }
        if failed:
            failure = ToolFailure(
                "OUTCOME_UNKNOWN" if unknown else "SERVICE_CALL_FAILED",
                "Some entity updates could not be confirmed.",
                status="outcome_unknown"
                if unknown
                else "confirmed"
                if succeeded or has_confirmed_changes
                else "not_started",
            )
            failure.data.update(
                {
                    **result,
                    "failed": failed,
                    "partial": bool(succeeded) or has_confirmed_changes,
                }
            )
            if not_attempted:
                failure.data["not_attempted"] = not_attempted
            raise failure
        return result

    async def _single(self, target: str, fields: dict[str, Any]) -> dict[str, Any]:
        await admit_registry_write(self.actor, self.authorize)
        target = _validated_payload(
            self.hass, "config/entity_registry/get", {"entity_id": target}
        )["entity_id"]
        self._references(
            {
                "area_id": clearable_value(fields.get("area_id"), "area_id"),
                "labels": fields["labels"]
                if fields["label_operation"] in {"set", "add"}
                else None,
                "categories": fields["categories"] or {},
            }
        )
        if fields.get("new_entity_id") is not None:
            apply_entity_rename(target, fields["new_entity_id"], {}, [])
        self._exposure_payloads(target, fields["expose_to"])
        registry = self._owner()
        initial = registry.async_get(target)
        if initial is None:
            if (
                fields["expose_to"] is not None
                and fields["categories"] is None
                and fields["labels"] is None
                and not any(
                    fields.get(key) is not None
                    for key in (
                        "area_id",
                        "name",
                        "use_device_name",
                        "icon",
                        "device_class",
                        "options",
                        "enabled",
                        "hidden",
                        "aliases",
                        "use_entity_name_alias",
                        "new_entity_id",
                        "new_device_name",
                    )
                )
            ):
                service = NativeLegacyExposure(
                    self.hass, self.actor, authorize=self.authorize, backup=self.backup
                )
                return await service.set(target, fields["expose_to"])
            raise ToolFailure(
                "SERVICE_CALL_FAILED",
                f"Entity '{target}' not found in registry",
                status="not_started",
            )
        identity = entity_registry_identity(registry_json(initial.extended_dict))
        async with AsyncExitStack() as owners:
            await owners.enter_async_context(
                registry_update_lock("entity_registry", identity["id"])
            )
            await admit_registry_write(self.actor, self.authorize)
            entry = self._current_entry(registry, identity)
            before = registry_json(entry.extended_dict)
            if before["entity_id"] != target:
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "The entity was renamed while waiting for its owner lock",
                    status="not_started",
                )
            plan = self._plan(registry, before, fields)
            await self._prepare_device(plan, owners)
            await self._capture_plan(plan)
            return self._commit_plan(plan)

    def _plan(
        self, registry: Any, before: dict[str, Any], fields: dict[str, Any]
    ) -> _EntityPlan:
        reject_name_with_use_device_name(
            fields.get("name"), fields.get("use_device_name")
        )
        if fields.get("use_device_name"):
            reject_use_device_name_without_device(before["entity_id"], before)
        message: dict[str, Any] = {"entity_id": before["entity_id"]}
        updates: list[str] = []
        build_name_visibility_fields(
            message,
            updates,
            fields.get("area_id"),
            fields.get("name"),
            fields.get("icon"),
            fields.get("device_class"),
            fields.get("use_device_name"),
        )
        aliases, note = merge_entity_aliases(
            before.get("aliases", []),
            fields["aliases"],
            fields.get("use_entity_name_alias"),
        )
        labels = merge_entity_labels(
            before.get("labels", []), fields["labels"], fields["label_operation"]
        )
        build_state_tag_fields(
            message,
            updates,
            fields.get("enabled"),
            fields.get("hidden"),
            aliases,
            fields["categories"],
            labels,
            fields["label_operation"],
            fields["labels"],
        )
        if note:
            updates.append(note)
        if fields.get("new_entity_id") is not None:
            apply_entity_rename(
                before["entity_id"], fields["new_entity_id"], message, updates
            )
        payloads = (
            [_validated_payload(self.hass, "config/entity_registry/update", message)]
            if len(message) > 1
            else []
        )
        target = fields.get("new_entity_id") or before["entity_id"]
        payloads.extend(
            _validated_payload(
                self.hass,
                "config/entity_registry/update",
                {"entity_id": target, "options_domain": domain, "options": values},
            )
            for domain, values in (fields["options"] or {}).items()
        )
        exposure = self._exposure_payloads(target, fields["expose_to"])
        references = {
            "area_id": message.get("area_id"),
            "labels": fields["labels"]
            if fields["label_operation"] in {"set", "add"}
            else None,
            "categories": fields["categories"] or {},
        }
        self._references(references)
        for payload in payloads:
            self._check_enable(registry.async_get(before["id"]), payload)
        return _EntityPlan(
            registry, before, payloads, exposure, references, updates, fields
        )

    def _exposure_payloads(
        self, target: str, exposure: dict[str, bool] | None
    ) -> list[dict[str, Any]]:
        try:
            payloads = [
                _validated_payload(
                    self.hass,
                    "homeassistant/expose_entity",
                    {
                        "assistants": [assistant],
                        "entity_ids": [target],
                        "should_expose": value,
                    },
                )
                for assistant, value in (exposure or {}).items()
            ]
        except ToolFailure as error:
            if error.code != "VALIDATION_FAILED":
                raise
            details = error.data["error"]
            assert isinstance(details, dict)
            payload = {
                **error.data,
                "error": {
                    **details,
                    "code": "VALIDATION_INVALID_PARAMETER",
                },
            }
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                str(error),
                data=payload,
                status=error.status,
            ) from error
        return sorted(payloads, key=lambda payload: not payload["should_expose"])

    async def _prepare_device(self, plan: _EntityPlan, owners: AsyncExitStack) -> None:
        from homeassistant.helpers import device_registry as dr

        raw = plan.fields.get("new_device_name")
        blank = raw is not None and not raw.strip()
        name = clearable_value(raw, "new_device_name", hint=_CLEAR_DEVICE)
        if blank:
            plan.warnings.append(_CLEAR_DEVICE)
        if name is None:
            if (
                not plan.payloads
                and not plan.exposure
                and plan.fields["expose_to"] is None
            ):
                raise_failure(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "new_device_name is blank and no other update was given"
                        if blank
                        else "No updates specified",
                        suggestions=[_CLEAR_DEVICE] if blank else None,
                    )
                )
            return
        identifier = plan.before.get("device_id")
        if not identifier:
            plan.device_result = {
                "warnings": ["Entity has no associated device — device rename skipped"]
            }
            return
        await owners.enter_async_context(registry_update_lock("device", identifier))
        await admit_registry_write(self.actor, self.authorize)
        registry = dr.async_get(self.hass)
        try:
            device = NativeDevices._target(registry, identifier)
        except ToolFailure as error:
            plan.device_result = {
                "device_id": identifier,
                "warnings": [
                    f"Entity updated but device rename could not be prepared: {error}"
                ],
            }
            return
        payload = prepare_device_update(
            self.hass,
            _validated_payload(
                self.hass,
                "config/device_registry/update",
                {"device_id": identifier, "name_by_user": name},
            ),
        )
        plan.device = registry, device, registry_json(device.dict_repr), payload

    def _check_plan(self, plan: _EntityPlan) -> Any:
        from homeassistant.helpers import device_registry as dr

        entry = self._current_entry(
            plan.registry, entity_registry_identity(plan.before)
        )
        if not json_values_equal(registry_json(entry.extended_dict), plan.before):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The entity changed after its compound before-image was read",
                status="not_started",
            )
        if plan.device is not None:
            registry, _, before, _ = plan.device
            device = registry.async_get(before["id"], include_composite_devices=False)
            if (
                dr.async_get(self.hass) is not registry
                or device is None
                or not json_values_equal(registry_json(device.dict_repr), before)
            ):
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "The device changed after its compound before-image was read",
                    status="not_started",
                )
        return entry

    async def _capture_plan(self, plan: _EntityPlan) -> None:
        await admit_registry_write(self.actor, self.authorize)
        self._check_plan(plan)
        if plan.payloads or plan.exposure:
            plan.warnings.extend(
                await capture_registry_before(
                    self.backup,
                    self.actor,
                    "entity_registry",
                    plan.before["id"],
                    plan.before,
                    "ha_set_entity",
                )
            )
            await admit_registry_write(self.actor, self.authorize)
            self._check_plan(plan)
        if plan.device is not None:
            plan.warnings.extend(
                await capture_registry_before(
                    self.backup,
                    self.actor,
                    "device",
                    plan.device[2]["id"],
                    plan.device[2],
                    "ha_set_entity",
                )
            )
            await admit_registry_write(self.actor, self.authorize)
            self._check_plan(plan)
        self._references(plan.references)

    def _phase_context(self, plan: _EntityPlan) -> dict[str, Any]:
        return {
            "entity_id": plan.before["entity_id"],
            "registry_id": plan.before["id"],
            "partial": bool(plan.confirmed),
            "confirmed_phases": list(plan.confirmed),
            "options_succeeded": dict(plan.options_succeeded),
            "exposure_succeeded": dict(plan.exposure_succeeded),
            "device_rename_succeeded": bool(
                plan.device_result and plan.device_result.get("success")
            ),
        }

    def _guard_phase(
        self, plan: _EntityPlan, payload: dict[str, Any] | None = None
    ) -> Any:
        try:
            check_mutation_admission(self.actor)
            entry = self._current_entry(
                plan.registry, entity_registry_identity(plan.before)
            )
            if payload is not None:
                self._check_enable(entry, payload)
            return entry
        except Exception as error:
            failure = registry_failure(error, {"entity_id": plan.before["entity_id"]})
            if plan.confirmed:
                failure.data.update(self._phase_context(plan))
                failure.status = "confirmed"
            raise failure from error

    def _commit_plan(self, plan: _EntityPlan) -> dict[str, Any]:
        for payload in plan.payloads:
            entry = self._guard_phase(plan, payload)
            try:
                written = self._write(plan.registry, entry, payload)
                entry = self._confirmed_entry(
                    plan.registry,
                    entity_registry_identity(plan.before),
                    written,
                    payload,
                )
                if (
                    payload.get("new_entity_id")
                    and entry.entity_id != payload["new_entity_id"]
                ):
                    raise RuntimeError(
                        "Core did not confirm the requested entity rename"
                    )
            except Exception as error:  # noqa: BLE001 - Core save/listeners may already have committed
                registry_outcome_unknown(
                    "entity", "update", error, context=self._phase_context(plan)
                )
            phase = payload.get("options_domain", "registry")
            plan.confirmed.append(phase)
            if "options_domain" in payload:
                effective, warning = self._effective_options(entry, payload)
                plan.options_succeeded[phase] = effective
                plan.updates.append(f"options[{phase}]={effective}")
                if warning:
                    plan.warnings.append(warning)
        self._commit_device(plan)
        self._commit_exposure(plan)
        row = registry_json(self._guard_phase(plan).extended_dict)
        result = entity_write_result(
            row["entity_id"],
            plan.before["entity_id"],
            row,
            plan.updates,
            plan.fields.get("new_entity_id"),
            plan.exposure_succeeded or None,
            plan.device_result,
            preserve_core_fields=True,
        )
        if plan.warnings:
            result.setdefault("warnings", []).extend(plan.warnings)
        if plan.options_succeeded:
            result["options_succeeded"] = plan.options_succeeded
        return result

    def _commit_device(self, plan: _EntityPlan) -> None:
        from homeassistant.helpers import device_registry as dr

        if plan.device is None:
            return
        entry = self._guard_phase(plan)
        registry, device, before, payload = plan.device
        if entry.device_id != before["id"]:
            failure = ToolFailure(
                "CONFIG_CONFLICT",
                "The entity's associated device changed before rename; no device callback was sent.",
                status="confirmed" if plan.confirmed else "not_started",
            )
            failure.data.update(self._phase_context(plan))
            raise failure
        current = registry.async_get(before["id"], include_composite_devices=False)
        if (
            dr.async_get(self.hass) is not registry
            or current is None
            or not json_values_equal(registry_json(current.dict_repr), before)
        ):
            failure = ToolFailure(
                "CONFIG_CONFLICT",
                "The device changed before its rename; no device callback was sent.",
                status="confirmed" if plan.confirmed else "not_started",
            )
            failure.data.update(self._phase_context(plan))
            raise failure
        try:
            commit_device_update(registry, current, payload)
            written = registry.async_get(before["id"], include_composite_devices=False)
            before_anchor = NativeDevices._removal_anchor(device)
            after_anchor = (
                NativeDevices._removal_anchor(written) if written is not None else None
            )
            if (
                dr.async_get(self.hass) is not registry
                or written is None
                or after_anchor is None
                or after_anchor[0] is not before_anchor[0]
                or not json_values_equal(after_anchor[1], before_anchor[1])
                or registry_json(written.dict_repr).get("name_by_user")
                != payload["name_by_user"]
            ):
                raise RuntimeError(
                    "Core did not confirm the same device identity after rename"
                )
        except Exception as error:  # noqa: BLE001 - device owner/listeners may have committed
            registry_outcome_unknown(
                "device", "rename", error, context=self._phase_context(plan)
            )
        plan.device_result = {"success": True, "device_id": before["id"]}
        plan.confirmed.append("device")
        plan.updates.append(f"device_name -> {payload['name_by_user']}")

    def _commit_exposure(self, plan: _EntityPlan) -> None:
        from homeassistant.components.homeassistant import exposed_entities

        for payload in plan.exposure:
            for assistant in payload["assistants"]:
                entry = self._guard_phase(plan)
                try:
                    exposed_entities.async_expose_entity(
                        self.hass, assistant, entry.entity_id, payload["should_expose"]
                    )
                    current = self._current_entry(
                        plan.registry, entity_registry_identity(plan.before)
                    )
                    if (
                        current.options.get(assistant, {}).get("should_expose")
                        is not payload["should_expose"]
                    ):
                        raise RuntimeError(
                            "Core did not confirm the registry exposure option"
                        )
                except Exception as error:  # noqa: BLE001 - Core exposure owner/listeners may have committed
                    registry_outcome_unknown(
                        "entity", "exposure", error, context=self._phase_context(plan)
                    )
                plan.exposure_succeeded[assistant] = payload["should_expose"]
                plan.confirmed.append(f"exposure:{assistant}")
        if plan.fields["expose_to"] is not None:
            plan.updates.append(f"expose_to={plan.fields['expose_to']}")
