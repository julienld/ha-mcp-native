"""Native old-style groups use actual Core entity owners and their callbacks."""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.json import json_bytes

from ha_mcp.runtime.group_values import (
    group_action_word,
    group_list_result,
    group_remove_result,
    group_set_result,
)
from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation
from ha_mcp.utils.owned_executor import await_owned_io
from ha_mcp.utils.registry_update_lock import registry_update_lock

from . import group_records as records
from .entity_waiters import wait_registered, wait_removed
from .registry_access import (
    Authorize,
    Capture,
    admit_registry_write,
    authorize_actor,
    capture_registry_before,
    registry_failure,
)

type GroupWarnings = list[str]


@dataclass(slots=True)
class NativeGroups:
    hass: HomeAssistant
    actor: CallContext
    authorize: Authorize = field(kw_only=True, repr=False)
    backup: Capture | None = field(default=None, kw_only=True, repr=False)

    async def list(self, limit: int, offset: int) -> dict[str, Any]:
        try:
            await authorize_actor(self.actor, self.authorize)
            states = [state.as_dict() for state in self.hass.states.async_all("group")]
            try:
                component = records.component(self.hass)
            except UnsupportedOperation:
                component = None
            modes = {}
            if component is not None:
                for state in states:
                    owner = records.target(component, state["entity_id"])
                    if owner is not None:
                        modes[state["entity_id"]] = owner.mode is all
            # Core values are detached on their owning loop; the worker receives
            # serialized bytes rather than State objects or live attribute maps.
            detached = json_bytes(states)

            def project() -> dict[str, Any]:
                rows = json.loads(detached)
                for row in rows:
                    if row["entity_id"] in modes:
                        row["attributes"]["all"] = modes[row["entity_id"]]
                return group_list_result(rows, limit, offset)

            result: dict[str, Any] = await await_owned_io(
                self.hass.async_add_executor_job(project)
            )
            await authorize_actor(self.actor, self.authorize)
            return result
        except Exception as error:
            raise registry_failure(error, {"operation": "list_groups"}) from error

    async def _admit(
        self, component: Any, entity_id: str, owner: Any, before: Any
    ) -> None:
        await admit_registry_write(self.actor, self.authorize)
        records.compare_target(self.hass, component, entity_id, owner, before)

    async def set(self, service_data: dict[str, Any], *, wait: bool) -> dict[str, Any]:
        try:
            await admit_registry_write(self.actor, self.authorize)
            fields = records.validate_service(self.hass, "set", service_data)
            entity_id = f"group.{fields['object_id']}"
            async with registry_update_lock("group", entity_id):
                component = records.component(self.hass)
                owner = records.target(component, entity_id)
                before = records.group_snapshot(owner)
                if owner is None and self.hass.states.get(entity_id) is not None:
                    raise ToolFailure(
                        "CONFIG_CONFLICT",
                        "The group ID is occupied by another state owner",
                        status="not_started",
                    )
                warnings = await self._capture(
                    fields["object_id"], before, "ha_config_set_group"
                )
                await self._admit(component, entity_id, owner, before)
                await await_owned_io(
                    self._set_owned(component, entity_id, owner, before, fields)
                )
                if wait and not await wait_registered(
                    self.hass, {"entity_id": entity_id}
                ):
                    warnings.append(
                        f"Group {group_action_word(fields)} but {entity_id} not yet queryable. It may take a moment to become available."
                    )
                return group_set_result(
                    fields, {"warnings": warnings} if warnings else {}
                )
        except Exception as error:
            raise registry_failure(
                error, {"object_id": service_data.get("object_id")}
            ) from error

    async def _capture(self, object_id: str, before: Any, source: str) -> GroupWarnings:
        return await capture_registry_before(
            self.backup,
            self.actor,
            "group",
            object_id,
            records.public_snapshot(before),
            source,
        )

    async def _set_owned(
        self,
        component: Any,
        entity_id: str,
        owner: Any,
        before: Any,
        fields: dict[str, Any],
    ) -> None:
        from homeassistant.components.group.entity import Group
        from homeassistant.core import Context

        await self._admit(component, entity_id, owner, before)
        if owner is None and self.hass.states.get(entity_id) is not None:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The group ID became occupied before creation",
                status="not_started",
            )
        context = Context(user_id=self.actor.user_id)
        try:
            if owner is None:
                # Core creates its actual Group, allocates its ID/order, and
                # installs all membership listeners through EntityComponent.
                owner = await Group.async_create_group(
                    self.hass,
                    fields.get("name", fields["object_id"]),
                    created_by_service=True,
                    entity_ids=fields.get("entities")
                    or fields.get("add_entities")
                    or None,
                    icon=fields.get("icon"),
                    mode=fields.get("all"),
                    object_id=fields["object_id"],
                    order=None,
                    context=context,
                )
                if owner.entity_id != entity_id:
                    raise RuntimeError("Core created a different group identity")
                self._same_owner(component, entity_id, owner)
                records.assert_group_values(
                    owner,
                    {
                        "object_id": fields["object_id"],
                        "name": fields.get("name", fields["object_id"]),
                        "entities": fields.get("entities")
                        or fields.get("add_entities")
                        or [],
                        "icon": fields.get("icon"),
                        "all": fields.get("all", False),
                        "created_by_service": True,
                    },
                )
                return
            self._update(component, entity_id, owner, fields, context)
        except (Exception, asyncio.CancelledError) as error:
            raise self._unknown(
                "set", entity_id, getattr(owner, "entity_id", None)
            ) from error

    def _same_owner(self, component: Any, entity_id: str, owner: Any) -> None:
        if (
            records.component(self.hass) is not component
            or records.target(component, entity_id) is not owner
        ):
            raise RuntimeError("The group owner changed during its callback")

    def _update(
        self,
        component: Any,
        entity_id: str,
        owner: Any,
        fields: dict[str, Any],
        context: Any,
    ) -> None:
        from .group_writes import apply_fields

        owner.async_set_context(context)
        apply_fields(
            owner,
            fields,
            lambda: self._guard_callback(component, entity_id, owner),
        )

    def _guard_callback(self, component: Any, entity_id: str, owner: Any) -> None:
        check_mutation_admission(self.actor)
        self._same_owner(component, entity_id, owner)

    async def remove(self, object_id: str, *, wait: bool) -> dict[str, Any]:
        try:
            await admit_registry_write(self.actor, self.authorize)
            fields = records.validate_service(
                self.hass, "remove", {"object_id": object_id}
            )
            entity_id = f"group.{fields['object_id']}"
            async with registry_update_lock("group", entity_id):
                component = records.component(self.hass)
                owner = records.target(component, entity_id)
                before = records.group_snapshot(owner)
                warnings = await self._capture(
                    object_id, before, "ha_config_remove_group"
                )
                await await_owned_io(
                    self._remove_owned(component, entity_id, owner, before)
                )
                result: dict[str, Any] = {"warnings": warnings} if warnings else {}
                if wait and not await wait_removed(self.hass, {"entity_id": entity_id}):
                    result.setdefault("warnings", []).append(
                        "Group removal completed but its state is still present; the mutation was not repeated."
                    )
                return group_remove_result(object_id, result)
        except Exception as error:
            raise registry_failure(error, {"object_id": object_id}) from error

    async def _remove_owned(
        self, component: Any, entity_id: str, owner: Any, before: Any
    ) -> None:
        from homeassistant.core import Context

        await self._admit(component, entity_id, owner, before)
        if owner is None:
            return
        owner.async_set_context(Context(user_id=self.actor.user_id))
        try:
            await component.async_remove_entity(entity_id)
            if (
                records.component(self.hass) is not component
                or records.target(component, entity_id) is not None
            ):
                raise RuntimeError("The group identity is still occupied after removal")
        except (Exception, asyncio.CancelledError) as error:
            raise self._unknown("remove", entity_id) from error

    @staticmethod
    def _unknown(
        action: str, entity_id: str, actual_id: str | None = None
    ) -> ToolFailure:
        detail = (
            {"actual_entity_id": actual_id}
            if actual_id and actual_id != entity_id
            else {}
        )
        return ToolFailure(
            "OUTCOME_UNKNOWN",
            f"Core group {action} started; inspect the current group before retrying.",
            data={
                "success": False,
                "entity_id": entity_id,
                "error": {
                    "code": "OUTCOME_UNKNOWN",
                    "message": "Core group changes may already be present.",
                },
                **detail,
            },
            status="outcome_unknown",
        )
