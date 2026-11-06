"""Finite todo reads, service writes, bridge calls and recovery captures."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tool_api import BackendTools, ToolFailure

from .errors import raise_failure as raise_tool_error
from .todo_values import _raise_core_failure, find_item, service_message


class TodoPorts(BackendTools):
    async def _list_state(self, entity_id: str) -> dict[str, Any]:
        """The list's state; an unknown list is not found."""
        state = await self._state(entity_id)
        if state is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.ENTITY_NOT_FOUND,
                    f"Home Assistant has no todo list {entity_id}",
                    context={"entity_id": entity_id},
                    suggestions=["Use ha_get_todo() to find valid todo lists"],
                )
            )
        return state

    async def _require_available(self, entity_id: str) -> None:
        """Core's services skip an unavailable list: a service call changes
        nothing yet answers success. Refuse a write up front."""
        if (await self._list_state(entity_id)).get("state") == "unavailable":
            raise_tool_error(
                create_error_response(
                    ErrorCode.ENTITY_UNAVAILABLE,
                    f"Todo list {entity_id} is unavailable; Home Assistant skips "
                    "an unavailable list, so nothing would be changed",
                    context={"entity_id": entity_id},
                    suggestions=["Check the integration that provides the list"],
                )
            )

    async def _write(
        self,
        label: str,
        message: dict[str, Any],
        context: dict[str, Any],
        applied: str = "",
    ) -> None:
        """Send one write; a write that got no answer is reported as unknown,
        and one never sent after an applied write says what was applied."""
        try:
            result = await self._send(message)
        except ToolFailure as error:
            if error.status == "not_started":
                if not applied:
                    raise
                raise_tool_error(
                    create_error_response(
                        ErrorCode.CONNECTION_FAILED,
                        f"{label} was not sent: {error}{applied}",
                        context=context,
                    )
                )
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"{label} got no answer, so whether it was applied is unknown: {error}{applied}",
                    context={**context, "write_outcome_unknown": True},
                    suggestions=["Check with ha_get_todo() before retrying"],
                )
            )
        if not result.get("success"):
            _raise_core_failure(label, result, context, applied)

    async def _todo_service(
        self, service: str, entity_id: str, data: dict[str, Any], applied: str = ""
    ) -> None:
        await self._write(
            f"todo.{service}",
            service_message(service, entity_id, data),
            {"entity_id": entity_id, **data},
            applied,
        )

    async def _list_items(self, entity_id: str) -> list[dict[str, Any]]:
        result = await self._send({"type": "todo/item/list", "entity_id": entity_id})
        if not result.get("success"):
            _raise_core_failure("todo/item/list", result, {"entity_id": entity_id})
        items = (result.get("result") or {}).get("items")
        if not isinstance(items, list):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"todo/item/list answered without an item list: {result}",
                    context={"entity_id": entity_id},
                )
            )
        return items

    async def _read_back(
        self, entity_id: str
    ) -> tuple[list[dict[str, Any]] | None, list[str]]:
        """The list after a write that Core applied. A failed read must not
        report that write as failed, or a retry repeats it."""
        try:
            return await self._list_items(entity_id), []
        except ToolFailure as e:
            return None, [
                f"The write was applied, but the list could not be read back: {e}"
            ]

    async def _resolve(
        self, entity_id: str, refs: list[str]
    ) -> tuple[list[dict[str, Any] | None], list[dict[str, Any]] | None]:
        """The row each ref names, the one Core's todo services act on, and
        the list as read, or None when the component resolved them."""
        reply = await self._bridge(entity_id, "resolve", refs=refs)
        if reply is not None:
            return reply["items"], None
        items = await self._list_items(entity_id)
        return [find_item(items, ref) for ref in refs], items

    async def _states(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self.backend.execute(
            "todo.states", {}, self.context
        )
        return result

    async def _state(self, entity_id: str) -> dict[str, Any] | None:
        result = await self.backend.execute(
            "states.read", {"entity_ids": [entity_id]}, self.context
        )
        if entity_id in result["states"]:
            state: dict[str, Any] = result["states"][entity_id]
            return state
        error = result.get("errors", {}).get(entity_id)
        if error and error.get("error", {}).get("code") not in {
            "ENTITY_NOT_FOUND",
            "RESOURCE_NOT_FOUND",
        }:
            raise_tool_error(error)
        return None

    async def _send(self, message: dict[str, Any]) -> dict[str, Any]:
        kind = message["type"]
        if kind == "todo/item/list":
            operation, arguments = "todo.items", {"entity_id": message["entity_id"]}
        elif kind == "todo/item/move":
            operation, arguments = (
                "todo.move",
                {key: value for key, value in message.items() if key != "type"},
            )
        elif kind == "call_service" and message.get("domain") == "todo":
            operation, arguments = (
                "todo.service",
                {
                    key: value
                    for key, value in message.items()
                    if key not in {"type", "domain"}
                },
            )
        else:
            raise ValueError("Unsupported todo operation")
        result: dict[str, Any] = await self.backend.execute(
            operation, arguments, self.context
        )
        return result

    async def _bridge(
        self, entity_id: str, action: str, **fields: Any
    ) -> dict[str, Any] | None:
        result: dict[str, Any] | None = await self.backend.execute(
            "todo.bridge",
            {"entity_id": entity_id, "action": action, **fields},
            self.context,
        )
        return result

    async def _bridge_write(
        self, entity_id: str, action: str, **fields: Any
    ) -> dict[str, Any]:
        reply = await self._bridge(entity_id, action, **fields)
        if reply is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.CONNECTION_FAILED,
                    f"Todo {action} was not sent",
                    context={"entity_id": entity_id},
                )
            )
        return reply

    async def _backup_items(
        self, entity_id: str, items: list[dict[str, Any] | None], source_tool: str
    ) -> list[str]:
        warnings: list[str] = []
        seen: set[str] = set()
        for item in items:
            if (
                not item
                or not (ref := item.get("uid") or item.get("summary"))
                or ref in seen
            ):
                continue
            seen.add(ref)
            result = await self.backend.execute(
                "todo.backup.capture",
                {
                    "entity_id": entity_id,
                    "item": ref,
                    "source_tool": source_tool,
                    "current_item": item,
                },
                self.context,
            )
            warnings.extend((result or {}).get("warnings", []))
        return warnings
