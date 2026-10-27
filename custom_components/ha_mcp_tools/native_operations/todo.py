"""Todo entity reads and service calls through Core's existing owners."""

from __future__ import annotations

import dataclasses
from importlib import import_module
from typing import Any

from ha_mcp.runtime.todo_targets import check_todo_target, todo_target
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation

TODO_OPERATIONS = frozenset(
    {
        "todo.states",
        "todo.items",
        "todo.item.prepare",
        "todo.item.add",
        "todo.item.update",
        "todo.item.remove",
        "todo.backup.capture",
    }
)


class NativeTodoOperations:
    def __init__(self, backend: Any) -> None:
        self.backend = backend

    async def execute(
        self, operation: str, arguments: dict[str, Any], actor: CallContext
    ) -> Any:
        await self.backend.authorize(actor)
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
