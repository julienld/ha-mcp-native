"""Admit a helper write, then let Core own its complete collection protocol."""

from __future__ import annotations

import copy
import dis
import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.helpers.json import json_bytes

from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation
from ha_mcp.utils.config_hash import canonical_json

from .registry_expectation import check_expected_registry

Admission = Callable[[CallContext], Awaitable[None]]


def fingerprint(value: Any) -> str:
    return canonical_json(json.loads(json_bytes(value)))


def require_non_suspending(method: Any) -> None:
    if not inspect.iscoroutinefunction(method) or any(
        instruction.opname in {"GET_AWAITABLE", "YIELD_VALUE", "SEND"}
        for instruction in dis.get_instructions(method)
    ):
        raise UnsupportedOperation("helpers.storage.suspending_validation")


def require_core_api(collection: Any, action: str) -> None:
    """Refuse overrides or a future suspension between validation and commit."""
    from homeassistant.helpers.collection import StorageCollection

    name = "async_create_item" if action == "create" else "async_update_item"
    method = getattr(type(collection), name, None)
    if not isinstance(collection, StorageCollection) or method is not getattr(
        StorageCollection, name
    ):
        raise UnsupportedOperation("helpers.storage.overridden_commit")
    instructions = tuple(dis.get_instructions(method))
    commit = next(
        (
            i
            for i, instruction in enumerate(instructions)
            if instruction.opname == "STORE_SUBSCR"
        ),
        None,
    )
    before = instructions[:commit] if commit is not None else ()
    awaits = [
        i
        for i, instruction in enumerate(before)
        if instruction.opname == "GET_AWAITABLE"
    ]
    validator = "_process_create_data" if action == "create" else "_update_data"
    if (
        commit is None
        or len(awaits) > 1
        or (
            awaits
            and not any(
                instruction.argval == validator for instruction in before[: awaits[0]]
            )
        )
    ):
        raise UnsupportedOperation("helpers.storage.changed_commit_protocol")


async def dispatch_core(
    actor: CallContext,
    admit: Admission,
    collection: Any,
    action: str,
    item_id: str | None,
    values: dict[str, Any],
    *,
    post_check: Callable[[], None] | None = None,
    registry_may_have_changed: bool = False,
) -> Any:
    completed = False
    try:
        result = (
            await collection.async_create_item(values)
            if action == "create"
            else await collection.async_update_item(item_id, values)
        )
        completed = True
        await admit(actor)
        if post_check is not None:
            post_check()
        return result
    except Exception as error:
        raise ToolFailure(
            "OUTCOME_UNKNOWN",
            "Core storage dispatch started; inspect the item before retrying",
            data={
                "success": False,
                "error": {
                    "code": "OUTCOME_UNKNOWN",
                    "message": "Storage dispatch did not complete normally",
                },
                "config_applied": True if completed else None,
                **(
                    {"registry_may_have_changed": True}
                    if registry_may_have_changed
                    else {}
                ),
            },
            status="outcome_unknown",
        ) from error


async def write_direct(
    actor: CallContext,
    admit: Admission,
    expected: Any,
    expected_registry: dict[str, Any] | None,
    hass: Any,
    collection: Any,
    action: str,
    item_id: str | None,
    fields: dict[str, Any],
) -> Any:
    require_core_api(collection, action)
    validator = (
        collection._process_create_data
        if action == "create"
        else collection._update_data
    )
    require_non_suspending(validator)
    values = copy.deepcopy(fields)
    before = fingerprint(collection.data)
    # These audited Core validators are pure and cannot suspend. Preflight
    # preserves validation errors before entering the side-effecting Core API.
    if action == "create":
        await validator(copy.deepcopy(values))
    else:
        current = collection.data.get(item_id)
        if current is None:
            raise ToolFailure(
                "RESOURCE_NOT_FOUND",
                "Stored helper no longer exists",
                status="not_started",
            )
        if "id" in values:
            raise ValueError("Cannot update ID")
        if expected is not None and fingerprint(current) != fingerprint(expected):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The helper changed after its snapshot was read",
                status="not_started",
            )
        await validator(copy.deepcopy(current), copy.deepcopy(values))
    await admit(actor)
    check_expected_registry(hass, expected_registry)
    if fingerprint(collection.data) != before:
        raise ToolFailure(
            "CONFIG_CONFLICT",
            "The helper collection changed during admission",
            status="not_started",
        )
    return await dispatch_core(
        actor,
        admit,
        collection,
        action,
        item_id,
        values,
        post_check=lambda: check_expected_registry(hass, expected_registry),
    )
