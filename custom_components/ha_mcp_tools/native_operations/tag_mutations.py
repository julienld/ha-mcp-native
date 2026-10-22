"""Tag validation mutates its registry; dispatch it only after final admission."""

from __future__ import annotations

import copy
import dis
from collections.abc import Awaitable, Callable
from typing import Any

from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation

from .helper_mutations import _fingerprint


def _require_non_suspending(method: Any) -> None:
    if any(
        instruction.opname in {"GET_AWAITABLE", "YIELD_VALUE", "SEND"}
        for instruction in dis.get_instructions(method)
    ):
        raise UnsupportedOperation("helpers.storage.tag.suspending_validation")


async def write_tag(
    actor: CallContext,
    admit: Callable[[CallContext], Awaitable[None]],
    expected: Any,
    collection: Any,
    action: str,
    item_id: str | None,
    fields: dict[str, Any],
    *,
    check_registry: Callable[[], None] | None = None,
) -> Any:
    from homeassistant.components.tag import (
        TAG_ID,
        TagIDExistsError,
        TagStorageCollection,
    )

    if type(collection) is not TagStorageCollection:
        raise UnsupportedOperation("helpers.storage.tag.overridden_validation")
    validator = (
        collection._process_create_data
        if action == "create"
        else collection._update_data
    )
    _require_non_suspending(validator)
    values = copy.deepcopy(fields)
    if action == "create":
        values.setdefault(TAG_ID, "")
        values = collection.CREATE_SCHEMA(values)
    else:
        values = collection.UPDATE_SCHEMA(values)
    await admit(actor)
    if check_registry is not None:
        check_registry()
    if action == "create":
        tag_id = values.get(TAG_ID)
        if tag_id and collection.id_manager.has_id(tag_id):
            # Core otherwise creates/renames the registry entry before its
            # ID manager detects the duplicate. Refuse before that side effect.
            raise TagIDExistsError(tag_id)
    else:
        current = collection.data.get(item_id)
        if current is None:
            raise ToolFailure(
                "RESOURCE_NOT_FOUND", "Tag no longer exists", status="not_started"
            )
        if expected is not None and _fingerprint(current) != _fingerprint(expected):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The tag changed after its snapshot was read",
                status="not_started",
            )
    return await _dispatch_tag(collection, action, item_id, values, actor, admit)


async def _dispatch_tag(
    collection: Any,
    action: str,
    item_id: str | None,
    values: dict[str, Any],
    actor: CallContext,
    admit: Callable[[CallContext], Awaitable[None]],
) -> Any:
    completed = False
    try:
        # Core 2026.10.2's tag validators cannot suspend (checked above).
        # Its original API therefore updates registry+store without a yield
        # between final admission and those changes, then awaits its listeners.
        result = (
            await collection.async_create_item(values)
            if action == "create"
            else await collection.async_update_item(item_id, values)
        )
        completed = True
        await admit(actor)
        return result
    except Exception as error:
        raise ToolFailure(
            "OUTCOME_UNKNOWN",
            "Tag dispatch started; inspect its registry and stored state before retrying",
            data={
                "success": False,
                "error": {
                    "code": "OUTCOME_UNKNOWN",
                    "message": "Tag dispatch did not complete normally",
                },
                "registry_may_have_changed": True,
                "config_applied": True if completed else None,
            },
            status="outcome_unknown",
        ) from error
