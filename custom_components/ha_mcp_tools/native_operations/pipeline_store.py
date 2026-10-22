"""Assist pipelines through the already-loaded Core storage collection.

Core 2026.10.2: assist_pipeline/pipeline.py publishes KEY_ASSIST_PIPELINE only
once async_load (including migrations/default creation) has completed. Never
invoke that initializer from a read. Registered validators are consulted without
calling their WebSocket handlers.
"""

import asyncio
import copy
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant

from ha_mcp.runtime.pipeline_values import (
    _drop_pipeline_id,
    validate_write,
    write_result,
)
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.owned_executor import await_owned_io
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .command_validation import _validated_payload
from .pipeline_conversation import PipelineConversation
from .registry_access import Authorize, admit_registry_write, authorize_actor

_PREFIX = "assist_pipeline/pipeline/"


def pipeline_owner(hass: HomeAssistant) -> Any:
    if "assist_pipeline" not in hass.config.components:
        raise ToolFailure(
            "SERVICE_UNAVAILABLE",
            "Core Assist pipelines are not loaded",
            status="not_started",
        )
    from homeassistant.components.assist_pipeline.pipeline import KEY_ASSIST_PIPELINE

    data = hass.data.get(KEY_ASSIST_PIPELINE)
    if data is None or isinstance(data, asyncio.Future):
        raise ToolFailure(
            "SERVICE_UNAVAILABLE",
            "Core Assist pipelines are not loaded yet",
            status="not_started",
        )
    return data.pipeline_store


def pipeline_image(owner: Any) -> dict[str, Any]:
    return copy.deepcopy(
        {
            "pipelines": {key: value.to_json() for key, value in owner.data.items()},
            "preferred": owner.async_get_preferred_item(),
        }
    )


def pin_pipeline(hass: HomeAssistant, owner: Any, before: dict[str, Any]) -> None:
    if pipeline_owner(hass) is not owner or not json_values_equal(
        pipeline_image(owner), before
    ):
        raise ToolFailure(
            "CONFIG_CONFLICT",
            "Assist pipeline owner or configuration changed",
            status="not_started",
        )


def _get(hass: HomeAssistant, pipeline_id: str | None) -> dict[str, Any]:
    from homeassistant.components.assist_pipeline.pipeline import async_get_pipeline

    return copy.deepcopy(async_get_pipeline(hass, pipeline_id).to_json())


def _required_id(action: str, pipeline_id: str | None) -> str:
    if pipeline_id is None:
        raise ToolFailure(
            "VALIDATION_MISSING_PARAMETER",
            f"action='{action}' requires pipeline_id",
            status="not_started",
        )
    return pipeline_id


def _unknown() -> ToolFailure:
    return ToolFailure(
        "OUTCOME_UNKNOWN",
        "Core Assist operation was dispatched; its final outcome is uncertain. Re-read before another change.",
        status="outcome_unknown",
    )


@dataclass(slots=True)
class NativePipelines(PipelineConversation):
    hass: HomeAssistant
    actor: CallContext
    authorize: Authorize = field(kw_only=True, repr=False)

    async def _manage_pipeline_read(
        self, *, action: str, pipeline_id: str | None
    ) -> dict[str, Any]:
        await authorize_actor(self.actor, self.authorize)
        owner = pipeline_owner(self.hass)
        if action == "list":
            pipelines = [copy.deepcopy(row.to_json()) for row in owner.data.values()]
            return {
                "success": True,
                "operation": "list",
                "count": len(pipelines),
                "pipelines": pipelines,
                "preferred_pipeline": owner.async_get_preferred_item(),
                "message": f"Found {len(pipelines)} Assist pipeline(s)",
            }
        _required_id(action, pipeline_id)
        data = _get(self.hass, pipeline_id)
        return {
            "success": True,
            "operation": "get",
            "pipeline_id": pipeline_id,
            "pipeline": data,
            "message": f"Found Assist pipeline: {data.get('name', pipeline_id)}",
        }

    async def _set_preferred_pipeline(self, pipeline_id: str | None) -> dict[str, Any]:
        identifier = _required_id("set_preferred", pipeline_id)
        await admit_registry_write(self.actor, self.authorize)
        async with registry_update_lock("assist_pipeline", "pipelines"):
            owner = pipeline_owner(self.hass)
            before = pipeline_image(owner)
            command = _PREFIX + "set_preferred"
            registration = self.hass.data.get("websocket_api", {}).get(command)
            payload = _validated_payload(
                self.hass, command, {"pipeline_id": identifier}
            )
            if identifier not in owner.data:
                raise ToolFailure(
                    "NOT_FOUND", "Assist pipeline not found", status="not_started"
                )
            await await_owned_io(self._prefer(owner, before, registration, payload))
        return {
            "success": True,
            "operation": "set_preferred",
            "pipeline_id": identifier,
            "message": f"Successfully set preferred Assist pipeline: {identifier}",
        }

    async def _guard(
        self, owner: Any, before: dict[str, Any], command: str, registration: Any
    ) -> None:
        await admit_registry_write(self.actor, self.authorize)
        pin_pipeline(self.hass, owner, before)
        if self.hass.data.get("websocket_api", {}).get(command) is not registration:
            raise ToolFailure(
                "CONFIG_CONFLICT", "Core Assist validator changed", status="not_started"
            )

    async def _prefer(
        self,
        owner: Any,
        before: dict[str, Any],
        registration: Any,
        payload: dict[str, Any],
    ) -> None:
        await self._guard(owner, before, _PREFIX + "set_preferred", registration)
        try:
            owner.async_set_preferred_item(payload["pipeline_id"])
            expected = {**before, "preferred": payload["pipeline_id"]}
            pin_pipeline(self.hass, owner, expected)
        except (Exception, asyncio.CancelledError) as error:
            raise _unknown() from error

    async def _write_pipeline(
        self,
        *,
        action: str,
        pipeline_id: str | None,
        base_pipeline_id: str | None,
        updates: dict[str, Any],
        make_preferred: bool,
    ) -> dict[str, Any]:
        validate_write(action, pipeline_id, updates, make_preferred)
        await admit_registry_write(self.actor, self.authorize)
        async with registry_update_lock("assist_pipeline", "pipelines"):
            owner = pipeline_owner(self.hass)
            before = pipeline_image(owner)
            source = _get(
                self.hass, pipeline_id if action == "update" else base_pipeline_id
            )
            command = _PREFIX + action
            registration = self.hass.data.get("websocket_api", {}).get(command)
            proposal = {**_drop_pipeline_id(source), **copy.deepcopy(updates)}
            if action == "update":
                proposal["pipeline_id"] = pipeline_id
                if pipeline_id not in owner.data:
                    raise ToolFailure(
                        "NOT_FOUND",
                        "Only stored Assist pipelines can be updated",
                        status="not_started",
                    )
            payload = _validated_payload(self.hass, command, proposal)
            return await await_owned_io(
                self._write(
                    owner, before, registration, action, payload, make_preferred
                )
            )

    async def _write(
        self,
        owner: Any,
        before: dict[str, Any],
        registration: Any,
        action: str,
        payload: dict[str, Any],
        make_preferred: bool,
    ) -> dict[str, Any]:
        from homeassistant.components.assist_pipeline.pipeline import (
            Pipeline,
            validate_language,
        )

        values = copy.deepcopy(payload)
        identifier = values.pop("pipeline_id", "")
        # Core owns both language validation and the model defaults. Freeze the
        # expected typed result before callbacks can replace the collection row.
        normalized = validate_language(copy.deepcopy(values))
        planned = copy.deepcopy(Pipeline(id=identifier, **normalized).to_json())
        await self._guard(owner, before, _PREFIX + action, registration)
        try:
            if action == "create":
                written = await owner.async_create_item(values)
            else:
                written = await owner.async_update_item(identifier, values)
            result = copy.deepcopy(written.to_json())
            await authorize_actor(self.actor, self.authorize)
            if (
                self.hass.data.get("websocket_api", {}).get(_PREFIX + action)
                is not registration
            ):
                raise RuntimeError("Core Assist validator changed after dispatch")
            expected = copy.deepcopy(before)
            if action == "create":
                if result["id"] in before["pipelines"]:
                    raise RuntimeError("Core reused an existing pipeline ID")
                planned["id"] = result["id"]
            if not json_values_equal(result, planned):
                raise RuntimeError("Pipeline changed during Core callbacks")
            expected["pipelines"][result["id"]] = planned
            pin_pipeline(self.hass, owner, expected)
            if make_preferred:
                # Core callbacks may await; renew live authorization before the
                # second distinct mutation, retaining the original owner/image.
                await admit_registry_write(self.actor, self.authorize)
                pin_pipeline(self.hass, owner, expected)
                owner.async_set_preferred_item(result["id"])
                expected["preferred"] = result["id"]
                pin_pipeline(self.hass, owner, expected)
        except (Exception, asyncio.CancelledError) as error:
            raise _unknown() from error
        return write_result(action, result, result["id"], make_preferred)
