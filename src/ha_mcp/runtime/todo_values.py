"""Todo result shaping and wrapper safeguards shared by every backend."""

import json
from typing import Any, NoReturn

from ha_mcp.config import get_global_settings
from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure as raise_tool_error
from .errors import ws_failure_code

__all__ = [
    "_NEW_ITEM_UNCLEAR",
    "_not_read_back",
    "_json_names",
    "_raise_core_failure",
    "_removed",
    "_refuse_namesake",
    "_require_todo_entity",
    "find_item",
    "unbacked_removals",
    "service_message",
]

_NEW_ITEM_UNCLEAR = "Could not tell the new item apart from other changes to the list; read it with ha_get_todo()"


def _not_read_back(item: str) -> str:
    return (
        f"'{item}' was not on the list read back after the write; "
        "read it with ha_get_todo()"
    )


def _json_names(value: str) -> list[str] | None:
    """The list a JSON array of strings holds, as some MCP clients send a
    list; None for any other string."""
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    if isinstance(parsed, list) and all(isinstance(i, str) for i in parsed):
        return parsed
    return None


def _raise_core_failure(
    command: str, result: dict[str, Any], context: dict[str, Any], applied: str = ""
) -> NoReturn:
    error = str(result.get("error", "Unknown error")).removeprefix("Command failed: ")
    raise_tool_error(
        create_error_response(
            ws_failure_code(result),
            f"{command} failed: {error}{applied}",
            context=context,
        )
    )


def _removed(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]] | None,
    refs: list[str] | None,
) -> list[dict[str, Any]] | None:
    """The asked-for items gone after a removal (``refs`` None: the completed
    ones); another client's removal of an item the call did not ask for is
    not this call's. None when the list was not read back."""
    if after is None:
        return None
    kept = {i.get("uid") for i in after}
    gone = [i for i in before if i.get("uid") not in kept]
    if refs is None:
        return [i for i in gone if i.get("status") == "completed"]
    return [i for i in gone if i.get("uid") in refs or i.get("summary") in refs]


def _refuse_namesake(entity_id: str, ref: str, item: str) -> NoReturn:
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"Another item on {entity_id} is named {ref!r}, so Home "
            "Assistant would change that one; nothing was changed",
            context={"entity_id": entity_id, "item": item},
            suggestions=["Rename the item named after the uid first"],
        )
    )


def _require_todo_entity(entity_id: str) -> None:
    if not entity_id.startswith("todo."):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Invalid entity_id: {entity_id}. Must start with 'todo.'",
                context={"entity_id": entity_id},
                suggestions=["Use ha_get_todo() to find valid todo list entity IDs"],
            )
        )


def find_item(items: list[dict[str, Any]], ref: str) -> dict[str, Any] | None:
    """The first item whose uid or summary is ``ref``, the one Core's todo
    services act on for the same value."""
    return next((i for i in items if ref in (i.get("uid"), i.get("summary"))), None)


def unbacked_removals(
    before: list[dict[str, Any]], after: list[dict[str, Any]] | None
) -> list[str]:
    """Why an item gone after ``remove_completed_items`` has no backup: Core
    picks the completed items when it runs the service, so one completed
    after the list was read and backed up is removed without a backup. An
    item another client deleted meanwhile is named the same way."""
    settings = get_global_settings()
    if after is None or not getattr(settings, "enable_auto_backup", False):
        return []
    kept = {i.get("uid") for i in after}
    return [
        f"'{i.get('summary')}' is gone from the list, but it was not completed "
        "when the list was read, so no backup of it was taken"
        for i in before
        if i.get("status") != "completed" and i.get("uid") not in kept
    ]


def service_message(
    service: str, todo_entity: str, data: dict[str, Any]
) -> dict[str, Any]:
    """A WebSocket ``call_service`` of todo.<service> on one list."""
    return {
        "type": "call_service",
        "domain": "todo",
        "service": service,
        "target": {"entity_id": todo_entity},
        "service_data": data,
    }
