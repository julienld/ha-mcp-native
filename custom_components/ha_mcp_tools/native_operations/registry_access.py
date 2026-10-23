"""Small actor and before-image helpers for direct Core registry services."""

import copy
import json
from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.helpers.json import json_bytes

from ha_mcp.runtime.error_classification import classified_error
from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.tool_api import CallContext, ToolFailure

Authorize = Callable[[CallContext], Awaitable[None]]
Capture = Callable[[dict[str, Any], CallContext], Awaitable[dict[str, Any] | None]]


async def authorize_actor(actor: CallContext, authorize: Authorize) -> None:
    try:
        await authorize(actor)
    except PermissionError as error:
        raise ToolFailure("AUTH_FORBIDDEN", str(error), status="not_started") from error


async def admit_registry_write(actor: CallContext, authorize: Authorize) -> None:
    await authorize_actor(actor, authorize)
    check_mutation_admission(actor)
    if actor.read_only:
        raise ToolFailure(
            "READ_ONLY", "Writes are disabled for this caller.", status="not_started"
        )


async def capture_registry_before(
    backup: Capture | None,
    actor: CallContext,
    domain: str,
    identifier: str | None,
    before: Any,
    source: str,
) -> list[str]:
    if backup is None or identifier is None or before is None:
        return []
    result = await backup(
        {
            "domain": domain,
            "source_tool": source,
            "backup_id": identifier,
            "current_config": copy.deepcopy(before),
        },
        actor,
    )
    return list(result.get("warnings") or []) if result else []


def registry_failure(error: Exception, context: dict[str, Any]) -> ToolFailure:
    if isinstance(error, ToolFailure):
        return (
            error
            if error.status is not None
            else ToolFailure(
                error.code, str(error), data=error.data, status="not_started"
            )
        )
    payload = classified_error(error, context)
    return ToolFailure(
        payload["error"]["code"],
        payload["error"]["message"],
        data=payload,
        status="not_started",
    )


def registry_json(value: Any) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(json_bytes(value))
    return result
