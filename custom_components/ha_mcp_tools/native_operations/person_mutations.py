"""Person's suspending user validation needs a final compare before Core commit."""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from typing import Any

from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation

from .registry_expectation import check_expected_registry
from .storage_api import fingerprint as _fingerprint


async def write_person(
    actor: CallContext,
    admit: Callable[[CallContext], Awaitable[None]],
    expected: Any,
    expected_registry: dict[str, Any] | None,
    hass: Any,
    collection: Any,
    action: str,
    item_id: str | None,
    fields: dict[str, Any],
) -> Any:
    from homeassistant.components.person import PersonStorageCollection

    if type(collection) is not PersonStorageCollection:
        raise UnsupportedOperation("helpers.storage.person.overridden_validation")
    from homeassistant.helpers.collection import (
        CHANGE_ADDED,
        CHANGE_UPDATED,
        CollectionChange,
        StorageCollection,
    )

    method_name = "async_create_item" if action == "create" else "async_update_item"
    if not isinstance(collection, StorageCollection) or getattr(
        type(collection), method_name
    ) is not getattr(StorageCollection, method_name):
        raise UnsupportedOperation("helpers.storage.overridden_commit")
    await admit(actor)
    check_expected_registry(hass, expected_registry)
    # Person user_id uniqueness checks inspect the whole collection.
    # A whole-collection check also protects those decisions across awaits.
    before = _fingerprint(collection.data)
    if action == "create":
        validated = await collection._process_create_data(copy.deepcopy(fields))
    else:
        if item_id not in collection.data:
            raise ToolFailure(
                "RESOURCE_NOT_FOUND",
                "Stored helper no longer exists",
                status="not_started",
            )
        if "id" in fields:
            raise ValueError("Cannot update ID")
        current = collection.data[item_id]
        if expected is not None and _fingerprint(current) != _fingerprint(expected):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The helper changed after its snapshot was read",
                status="not_started",
            )
        validated = await collection._update_data(
            copy.deepcopy(current), copy.deepcopy(fields)
        )
    await admit(actor)
    check_expected_registry(hass, expected_registry)
    if _fingerprint(collection.data) != before:
        raise ToolFailure(
            "CONFIG_CONFLICT",
            "The helper collection changed during validation; no mutation was sent",
            status="not_started",
        )
    # Core 2026.10.2 StorageCollection protocol: there is no await between
    # the last admission/compare and this commit. Keep Core's own ID,
    # serialization, save scheduling and listener implementations.
    if action == "create":
        item_id = collection.id_manager.generate_id(
            collection._get_suggested_id(validated)
        )
        item = collection._create_item(item_id, validated)
        change = CHANGE_ADDED
    else:
        item = validated
        change = CHANGE_UPDATED
    collection.data[item_id] = item
    try:
        collection._async_schedule_save()
        await collection.notify_changes(
            [
                CollectionChange(
                    change,
                    item_id,
                    item,
                    collection._hash_item(collection._serialize_item(item_id, item)),
                )
            ]
        )
        # The common facade applies registry changes after this returns.
        # Revocation while listeners ran leaves the committed item intact.
        await admit(actor)
        check_expected_registry(hass, expected_registry)
        return item if action == "create" else collection.data[item_id]
    except Exception as error:
        raise ToolFailure(
            "OUTCOME_UNKNOWN",
            "The item was stored before completion failed; inspect its state before retrying",
            data={
                "success": False,
                "error": {
                    "code": "OUTCOME_UNKNOWN",
                    "message": "Completion failed after storing the item",
                },
                "config_applied": True,
            },
            status="outcome_unknown",
        ) from error
