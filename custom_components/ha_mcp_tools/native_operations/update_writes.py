"""Finite Update service and Repair registry operations; no transport replay."""

import asyncio
import copy
from typing import Any

from homeassistant.core import Context

from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.runtime.update_plans import repair_items, update_targets
from ha_mcp.runtime.update_values import update_batch_result
from ha_mcp.tool_api import ToolFailure, UnsupportedOperation
from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.owned_executor import await_owned_io
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .command_validation import _validated_payload
from .registry_access import admit_registry_write, registry_failure
from .update_entities import (
    UpdateTarget,
    check_update_target,
    protected_update,
    stopping_update,
)
from .update_reads import UpdateReads


class UpdateWrites(UpdateReads):
    async def _admit(self) -> None:
        await admit_registry_write(self.actor, self.manager.backend.authorize)

    async def _updates(
        self,
        action: str,
        entity_ids: list[str] | str | None,
        categories: list[str] | str | None,
        backup: bool,
    ) -> dict[str, Any]:
        ids, cats = update_targets(action, entity_ids, categories)
        if cats:
            listing = await self._list(False)
            ids = [
                row["entity_id"]
                for category in cats
                for row in listing["categories"].get(category, [])
                if row["can_install"]
            ]
        results: list[dict[str, Any]] = []
        succeeded = 0
        stopped = False
        unknown = False
        for entity_id in ids:
            if stopped:
                results.append(
                    {"entity_id": entity_id, "success": False, "not_attempted": True}
                )
                continue
            try:
                result = await self._update(action, entity_id, backup, bool(cats))
            except Exception as error:  # noqa: BLE001 - preserve per-item failures and stop the tail
                failure = registry_failure(error, {"entity_id": entity_id})
                results.append({**failure.data, "entity_id": entity_id})
                unknown |= failure.status == "outcome_unknown"
                stopped = failure.code != "ENTITY_NOT_FOUND"
            else:
                succeeded += 1
                results.append(result)
                stopped = bool(result.get("scheduled"))
        await self._finish_authority(succeeded, unknown)
        result = update_batch_result(action, len(ids), results, succeeded)
        if unknown:
            result["operation_outcome"] = "outcome_unknown"
        elif ids and not succeeded:
            result["operation_outcome"] = "not_started"
        return result

    async def _update(
        self, action: str, entity_id: str, backup: bool, batch: bool
    ) -> dict[str, Any]:
        async with registry_update_lock("update", entity_id):
            target = await self._visible_target(entity_id)
            if (
                action == "install"
                and batch
                and protected_update(self.manager.hass, target)
            ):
                raise ToolFailure(
                    "VALIDATION_INVALID_PARAMETER",
                    "Core, OS and Supervisor updates must be targeted individually, including after an entity rename.",
                    status="not_started",
                )
            service = self.manager.hass.services.async_services_for_domain(
                "update"
            ).get(action)
            if service is None or not callable(service.schema):
                raise UnsupportedOperation(f"Core update.{action} service")
            data: dict[str, Any] = {"entity_id": entity_id}
            if action == "install" and backup:
                data["backup"] = True
            service.schema(copy.deepcopy(data))
            if action == "install" and stopping_update(self.manager.hass, target):
                await self._admit()
                check_update_target(self.manager.hass, target)
                self.manager.system_lifecycle.schedule(
                    "update_install",
                    self.actor,
                    update=target,
                    backup=backup,
                    update_service=service,
                )
                return {
                    "success": True,
                    "entity_id": entity_id,
                    "requested": True,
                    "scheduled": True,
                    "message": "The Core/OS update is scheduled and revalidated before dispatch. This runtime cannot confirm completion after its process stops; remaining batch targets were not started.",
                }
            return await await_owned_io(self._dispatch(target, action, service, data))

    async def _dispatch(
        self, target: UpdateTarget, action: str, service: Any, data: dict[str, Any]
    ) -> dict[str, Any]:
        await self._admit()
        await self._visible_target(target.entity_id)
        check_update_target(self.manager.hass, target)
        if (
            self.manager.hass.services.async_services_for_domain("update").get(action)
            is not service
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The Core update service changed",
                status="not_started",
            )
        check_mutation_admission(self.actor)
        expected_skipped = target.entity.latest_version if action == "skip" else None
        try:
            await self.manager.hass.services.async_call(
                "update",
                action,
                copy.deepcopy(data),
                blocking=True,
                context=Context(user_id=self.actor.user_id),
            )
            await self._visible_target(target.entity_id)
            check_update_target(self.manager.hass, target)
            if action in {"skip", "clear_skipped"}:
                state = self.manager.hass.states.get(target.entity_id)
                skipped = state.as_dict()["attributes"].get("skipped_version")
                if not json_values_equal(skipped, expected_skipped):
                    raise RuntimeError(
                        "The skipped version did not match the Core request"
                    )
        except (Exception, asyncio.CancelledError) as error:
            raise ToolFailure(
                "OUTCOME_UNKNOWN",
                "The Core update service was dispatched but its outcome could not be verified. It was not replayed.",
                status="outcome_unknown",
            ) from error
        return {
            "success": True,
            "entity_id": target.entity_id,
            "requested": True,
            "in_progress": target.entity.in_progress,
            "installed_version": target.entity.installed_version,
            "latest_version": target.entity.latest_version,
        }

    async def _finish_authority(self, succeeded: int, unknown: bool) -> None:
        try:
            await self._authorize()
        except Exception as error:
            failure = registry_failure(error, {"operation": "updates"})
            raise ToolFailure(
                failure.code,
                "Authority changed during the operation; inspect the current state before another write.",
                data={**failure.data, "confirmed_count": succeeded},
                status="outcome_unknown"
                if unknown
                else "confirmed"
                if succeeded
                else "not_started",
            ) from error

    async def _repairs(
        self, action: str, repairs: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        items = repair_items(action, repairs)
        results: list[dict[str, Any]] = []
        succeeded, unknown, stopped = 0, False, False
        for item in items:
            if stopped:
                results.append({**item, "success": False, "not_attempted": True})
                continue
            try:
                await self._repair(item, action == "ignore_repair")
            except Exception as error:  # noqa: BLE001 - preserve per-item failures and stop the tail
                failure = registry_failure(error, item)
                if failure.code == "RESOURCE_NOT_FOUND":
                    details = failure.data["error"]
                    assert isinstance(details, dict)
                    details["code"] = "SERVICE_CALL_FAILED"
                results.append({**item, **failure.data})
                unknown |= failure.status == "outcome_unknown"
                stopped = failure.code != "RESOURCE_NOT_FOUND"
            else:
                succeeded += 1
                results.append(
                    {"success": True, **item, "ignored": action == "ignore_repair"}
                )
        await self._finish_authority(succeeded, unknown)
        result = update_batch_result(action, len(items), results, succeeded)
        if unknown:
            result["operation_outcome"] = "outcome_unknown"
        elif not succeeded:
            result["operation_outcome"] = "not_started"
        return result

    async def _repair(self, item: dict[str, Any], ignore: bool) -> None:
        from homeassistant.helpers import issue_registry as ir

        values = _validated_payload(
            self.manager.hass, "repairs/ignore_issue", {**item, "ignore": ignore}
        )
        owner = ir.async_get(self.manager.hass)
        issue = owner.async_get_issue(values["domain"], values["issue_id"])
        if issue is None or (ignore and not issue.active):
            raise ToolFailure(
                "RESOURCE_NOT_FOUND", "Repairs issue not found", status="not_started"
            )
        await self._admit()
        if (
            ir.async_get(self.manager.hass) is not owner
            or owner.async_get_issue(values["domain"], values["issue_id"]) is not issue
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The repairs issue changed before dispatch",
                status="not_started",
            )
        try:
            written = owner.async_ignore(values["domain"], values["issue_id"], ignore)
            if (
                ir.async_get(self.manager.hass) is not owner
                or owner.async_get_issue(values["domain"], values["issue_id"])
                is not written
            ):
                raise RuntimeError("The repairs issue changed during Core callbacks")
        except (Exception, asyncio.CancelledError) as error:
            raise ToolFailure(
                "OUTCOME_UNKNOWN",
                "The repairs change was dispatched and was not replayed.",
                status="outcome_unknown",
            ) from error
