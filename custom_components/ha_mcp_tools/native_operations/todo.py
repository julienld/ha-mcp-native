"""Todo entity reads and service calls through Core's existing owners."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
from importlib import import_module
from typing import Any

from homeassistant.core import Context

from ha_mcp.runtime.todo_targets import check_todo_target, todo_target
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation
from ha_mcp.utils.config_hash import json_values_equal

from .. import todo_items
from .command_validation import _validated_payload
from .registry_access import admit_registry_write, authorize_actor, registry_failure

TODO_OPERATIONS = frozenset(
    {
        "todo.states",
        "todo.items",
        "todo.item.prepare",
        "todo.item.add",
        "todo.item.update",
        "todo.item.remove",
        "todo.backup.capture",
        "todo.bridge",
        "todo.move",
        "todo.service",
    }
)


class NativeTodoOperations:
    def __init__(self, backend: Any) -> None:
        self.backend = backend

    async def execute(
        self, operation: str, arguments: dict[str, Any], actor: CallContext
    ) -> Any:
        await self.backend.authorize(actor)
        if operation == "todo.bridge":
            return await self._bridge(arguments, actor)
        if operation == "todo.move":
            return await self._move(arguments, actor)
        if operation == "todo.service":
            return await self._native_service(arguments, actor)
        if operation == "todo.states":
            return [
                state.as_dict()
                for state in self.backend.hass.states.async_all()
                if state.entity_id.startswith("todo.")
            ]
        if operation == "todo.items":
            return self._items(arguments["entity_id"])
        if operation == "todo.item.prepare":
            return todo_target(
                self._item_list(arguments["entity_id"]), arguments["item"]
            )
        if operation == "todo.backup.capture":
            return await self.backend._capture_backup(operation, arguments, actor)
        if operation in {"todo.item.add", "todo.item.update", "todo.item.remove"}:
            return await self._service(
                operation.rsplit(".", 1)[1],
                arguments["data"],
                actor,
                arguments.get("expected_item"),
            )
        raise UnsupportedOperation(operation)

    def _loaded(self, entity_id: str) -> tuple[Any, Any]:
        core = todo_items._core()
        if core is None:
            raise UnsupportedOperation("Core todo bridge owners")
        component = self.backend.hass.data.get(core.component_key)
        entity = component.get_entity(entity_id) if component is not None else None
        if not isinstance(entity, core.list_entity):
            raise ToolFailure(
                "ENTITY_UNAVAILABLE",
                "Core has no loaded todo list under this ID",
                status="not_started",
            )
        return core, entity

    @staticmethod
    def _expected(core: Any, entity: Any, reference: str, expected: Any) -> str:
        if not isinstance(expected, dict):
            raise ToolFailure(
                "INVALID_ARGUMENTS",
                "A captured todo item is required",
                status="not_started",
            )
        selector = expected.get("uid") or reference
        current = core.find(selector, entity.todo_items)
        if current is None or not json_values_equal(
            todo_items._row(core, current), expected
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The todo item changed after it was read",
                status="not_started",
            )
        return str(selector)

    def _bridge_targets(self, core: Any, entity: Any, message: dict[str, Any]) -> None:
        if message["action"] == "update":
            self._expected(
                core, entity, message["uid"], message.pop("expected_item", None)
            )
        elif message["action"] == "remove":
            expected = message.pop("expected_items", None)
            if not isinstance(expected, list) or len(expected) != len(message["refs"]):
                raise ToolFailure(
                    "INVALID_ARGUMENTS",
                    "Each removal requires its captured todo item",
                    status="not_started",
                )
            message["refs"] = [
                self._expected(core, entity, ref, row)
                for ref, row in zip(message["refs"], expected, strict=True)
            ]

    @staticmethod
    def _bridge_failure(reply: dict[str, Any]) -> None:
        if reply.get("success"):
            return
        error = reply.get("error") or {}
        code = {
            "unsupported": "UNSUPPORTED_OPERATION",
            "not_loaded": "ENTITY_UNAVAILABLE",
            "list_unavailable": "ENTITY_UNAVAILABLE",
            "item_not_found": "RESOURCE_NOT_FOUND",
            "namesake": "CONFIG_CONFLICT",
            "not_supported": "VALIDATION_INVALID_PARAMETER",
            "invalid": "VALIDATION_INVALID_PARAMETER",
        }.get(error.get("code") or "")
        raise ToolFailure(
            code or "OUTCOME_UNKNOWN",
            str(error.get("message", "Todo outcome unknown")),
            status="not_started" if code else "outcome_unknown",
        )

    async def _bridge(
        self, arguments: dict[str, Any], actor: CallContext
    ) -> dict[str, Any]:
        message = copy.deepcopy(arguments)
        action, entity_id = message["action"], message["entity_id"]
        core, original = self._loaded(entity_id)
        if action == "resolve":
            await authorize_actor(actor, self.backend.authorize)
        else:
            await admit_registry_write(actor, self.backend.authorize)
        core, entity = self._loaded(entity_id)
        if entity is not original:
            raise ToolFailure(
                "CONFIG_CONFLICT", "Todo list owner changed", status="not_started"
            )
        self._bridge_targets(core, entity, message)
        previous = message.pop("previous_uid", None)
        moves = action == "update" and "previous_uid" in arguments
        if moves and not (entity.supported_features or 0) & core.can_move:
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                "List does not support reordering",
                status="not_started",
            )
        message["ha_mcp_context"] = Context(user_id=actor.user_id)
        try:
            reply = await todo_items.async_todo_item(self.backend.hass, message)
        except (Exception, asyncio.CancelledError) as error:
            raise ToolFailure(
                "OUTCOME_UNKNOWN",
                "Todo dispatch did not return its outcome; inspect before retrying",
                status="outcome_unknown",
            ) from error
        self._bridge_failure(reply)
        if action == "resolve":
            for reference, row in zip(message["refs"], reply["items"], strict=True):
                if row is not None:
                    self._expected(core, entity, reference, row)
        if moves:
            await self._move_after_update(
                entity_id, arguments["uid"], previous, reply, actor
            )
        return reply

    async def _move_after_update(
        self,
        entity_id: str,
        uid: str,
        previous: str | None,
        reply: dict[str, Any],
        actor: CallContext,
    ) -> None:
        arguments = {"entity_id": entity_id, "uid": uid, "expected_item": reply["item"]}
        if previous is not None:
            arguments["previous_uid"] = previous
        try:
            await self._move(arguments, actor)
        except (Exception, asyncio.CancelledError) as error:
            raise ToolFailure(
                "OUTCOME_UNKNOWN",
                "The update was applied, but its following move did not complete; inspect before retrying",
                status="outcome_unknown",
            ) from error

    async def _move(
        self, arguments: dict[str, Any], actor: CallContext
    ) -> dict[str, Any]:
        # Core 2026.10.4 todo/__init__.py uses this validator and owner method.
        payload = {
            key: value for key, value in arguments.items() if key != "expected_item"
        }
        command = "todo/item/move"
        registration = self.backend.hass.data.get("websocket_api", {}).get(command)
        payload = _validated_payload(self.backend.hass, command, payload)
        core, original = self._loaded(payload["entity_id"])
        before = [todo_items._row(core, item) for item in original.todo_items or ()]
        await admit_registry_write(actor, self.backend.authorize)
        core, entity = self._loaded(payload["entity_id"])
        if (
            entity is not original
            or self.backend.hass.data["websocket_api"].get(command) is not registration
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Todo owner or validator changed",
                status="not_started",
            )
        if not json_values_equal(
            before, [todo_items._row(core, item) for item in entity.todo_items or ()]
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Todo list changed during move preparation",
                status="not_started",
            )
        if "expected_item" in arguments:
            self._expected(core, entity, payload["uid"], arguments["expected_item"])
        if not entity.available:
            raise ToolFailure(
                "ENTITY_UNAVAILABLE", "Todo list is unavailable", status="not_started"
            )
        if not (entity.supported_features or 0) & core.can_move:
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                "List does not support reordering",
                status="not_started",
            )
        try:
            await entity.async_move_todo_item(
                uid=payload["uid"], previous_uid=payload.get("previous_uid")
            )
        except (Exception, asyncio.CancelledError) as error:
            raise ToolFailure(
                "OUTCOME_UNKNOWN",
                "Todo move outcome is unknown; inspect before retrying",
                status="outcome_unknown",
            ) from error
        return {"success": True, "result": None}

    async def _native_service(
        self, arguments: dict[str, Any], actor: CallContext
    ) -> dict[str, Any]:
        command = "call_service"
        registration = self.backend.hass.data.get("websocket_api", {}).get(command)
        payload = _validated_payload(
            self.backend.hass, command, {**arguments, "domain": "todo"}
        )
        entity_id = payload["target"]["entity_id"]
        if isinstance(entity_id, list) and len(entity_id) == 1:
            entity_id = entity_id[0]
        if not isinstance(entity_id, str):
            raise ToolFailure(
                "INVALID_ARGUMENTS",
                "This port targets one todo list",
                status="not_started",
            )
        _core, original = self._loaded(entity_id)
        writes = payload["service"] != "get_items"
        if writes:
            await admit_registry_write(actor, self.backend.authorize)
        else:
            await authorize_actor(actor, self.backend.authorize)
        _core, entity = self._loaded(entity_id)
        if (
            entity is not original
            or self.backend.hass.data["websocket_api"].get(command) is not registration
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Todo owner or service validator changed",
                status="not_started",
            )
        if writes and not entity.available:
            raise ToolFailure(
                "ENTITY_UNAVAILABLE", "Todo list is unavailable", status="not_started"
            )
        try:
            result = await self.backend.hass.services.async_call(
                "todo",
                payload["service"],
                payload.get("service_data", {}),
                target=payload["target"],
                blocking=True,
                return_response=payload.get("return_response", False),
                context=Context(user_id=actor.user_id),
            )
        except (Exception, asyncio.CancelledError) as error:
            if not writes and isinstance(error, Exception):
                raise registry_failure(error, {"entity_id": entity_id}) from error
            raise ToolFailure(
                "OUTCOME_UNKNOWN",
                "Todo service outcome is unknown; inspect before retrying",
                status="outcome_unknown",
            ) from error
        return {"success": True, "result": {"response": result}}

    def _items(self, entity_id: str) -> dict[str, Any]:
        todo = import_module("homeassistant.components.todo")
        # Core 2026.10 made its existing WebSocket/service serializer public.
        factory = getattr(todo, "api_items_factory", None) or getattr(
            todo, "_api_items_factory", None
        )
        if factory is None:
            raise ToolFailure(
                "UNSUPPORTED_OPERATION",
                "Core's to-do item API serializer is unavailable. Update Home Assistant before changing to-do items.",
                status="not_started",
            )

        component = self.backend.hass.data.get(todo.DATA_COMPONENT)
        entity = component.get_entity(entity_id) if component is not None else None
        if not isinstance(entity, todo.TodoListEntity):
            return {
                "success": False,
                "error": "Entity not found",
                "error_code": "not_found",
            }
        return {
            "success": True,
            "result": {
                "items": [
                    dataclasses.asdict(item, dict_factory=factory)
                    for item in entity.todo_items or []
                ]
            },
        }

    def _item_list(self, entity_id: str) -> list[dict[str, Any]]:
        result = self._items(entity_id)
        if not result["success"]:
            raise ToolFailure(
                "RESOURCE_NOT_FOUND", "Todo entity not found.", status="not_started"
            )
        items: list[dict[str, Any]] = result["result"]["items"]
        return items

    async def _service(
        self,
        action: str,
        data: dict[str, Any],
        actor: CallContext,
        expected_item: dict[str, Any] | None,
    ) -> Any:
        await self.backend.authorize(actor)
        if action != "add":
            if expected_item is None:
                raise ToolFailure(
                    "INVALID_ARGUMENTS",
                    "The expected todo item is required.",
                    status="not_started",
                )
            check_todo_target(
                self._item_list(data["entity_id"]), data["item"], expected_item
            )
        try:
            # The checked owner runs synchronously up to Core's service dispatch.
            # Core/provider awaits after that remain an external concurrency boundary.
            # Re-entering execute() would await authorization after the target CAS.
            response = await self.backend._execute_core_operation(
                "services.call_observe",
                {
                    "domain": "todo",
                    "service": action + "_item",
                    "service_data": data,
                    "entity_ids": [data["entity_id"]],
                    "wait": False,
                    "timeout": 10.0,
                    "return_response": False,
                    "expected_state": None,
                    "include_changed_states": True,
                },
                actor,
            )
        except ToolFailure:
            raise
        except Exception as error:
            # The service may already have reached an external todo provider.
            # Never translate that uncertainty into a pre-write validation error.
            raise ToolFailure(
                "OUTCOME_UNKNOWN",
                "Todo dispatch did not complete normally; inspect the list before retrying",
                status="outcome_unknown",
            ) from error
        if response.get("partial") or response.get("unknown_outcome"):
            raise ToolFailure(
                "OUTCOME_UNKNOWN",
                "Todo dispatch was not confirmed; inspect the list before retrying",
                status="outcome_unknown",
            )
        return response.get("changed_states", [])
