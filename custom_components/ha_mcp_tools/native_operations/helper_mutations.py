"""Keep Core's storage validation and notification protocol, fencing its commit."""

from __future__ import annotations

import copy
import json
from functools import partial
from typing import Any

from homeassistant.helpers.json import json_bytes

from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation
from ha_mcp.utils.config_hash import canonical_json

from ..helper_collections import async_write_item
from .owner import CoreOperationOwner


def _fingerprint(value: Any) -> str:
    return canonical_json(json.loads(json_bytes(value)))


class NativeHelperMutations(CoreOperationOwner):
    async def execute(
        self, arguments: dict[str, Any], actor: CallContext
    ) -> dict[str, Any]:
        from homeassistant.helpers import entity_registry as er

        writer = partial(self._write, actor, arguments.get("expected_item"))
        if arguments["helper_type"] == "tag":
            from .tag_mutations import write_tag

            writer = partial(
                write_tag, actor, self._admit, arguments.get("expected_item")
            )
        return await async_write_item(
            self.hass,
            er.async_get(self.hass),
            arguments,
            writer=writer,
        )

    async def _admit(self, actor: CallContext) -> None:
        try:
            await self.authorize(actor)
            check_mutation_admission(actor)
        except PermissionError as error:
            raise ToolFailure(
                "AUTH_REQUIRED",
                "Administrator admission was revoked",
                status="not_started",
            ) from error
        except ToolFailure as error:
            raise ToolFailure(
                error.code, str(error), data=error.data, status="not_started"
            ) from error
        if actor.read_only:
            raise ToolFailure("READ_ONLY", "Writes are disabled", status="not_started")

    async def _write(
        self,
        actor: CallContext,
        expected: Any,
        collection: Any,
        action: str,
        item_id: str | None,
        fields: dict[str, Any],
    ) -> Any:
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
        await self._admit(actor)
        # Some validators (person's user_id uniqueness) inspect other items.
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
        await self._admit(actor)
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
                        collection._hash_item(
                            collection._serialize_item(item_id, item)
                        ),
                    )
                ]
            )
            # The common facade applies registry changes after this returns.
            # Revocation while listeners ran leaves the committed item intact.
            await self._admit(actor)
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
