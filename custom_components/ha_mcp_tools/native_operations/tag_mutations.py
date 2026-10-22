"""Tag validation mutates its registry; dispatch it only after final admission."""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from typing import Any

from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation

from .storage_api import (
    dispatch_core,
    require_core_api,
)
from .storage_api import (
    fingerprint as _fingerprint,
)
from .storage_api import (
    require_non_suspending as _require_non_suspending,
)


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
    require_core_api(collection, action)
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
    return await dispatch_core(
        actor,
        admit,
        collection,
        action,
        item_id,
        values,
        registry_may_have_changed=True,
    )
