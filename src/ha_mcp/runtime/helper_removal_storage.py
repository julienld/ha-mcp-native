"""Shared storage-helper removal, classification and confirmed-delete responses."""

import logging
from typing import Any, NoReturn, cast

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tool_api import ToolFailure
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .errors import raise_classified, raise_failure, ws_failure_code
from .helper_storage import HelperStorageTools
from .helper_targets import not_stored_reply, raise_unregistered_entity_error

logger = logging.getLogger(__name__)


def removal_reply(
    response: dict[str, Any], entity: str | None, reason: str | None
) -> dict[str, Any]:
    """A removal's reply without a guessed entity, saying why it has none."""
    if entity is None:
        response["message"] = (
            f"Successfully deleted {response['helper_type']}: {response['target']}."
        )
        response.setdefault("warnings", []).append(
            f"{response['helper_type']} {response['target']} was deleted, but "
            f"{reason}, so its entity is not named and the removal was not waited on."
        )
    return response


def _raise_registry_read_failure(
    target: str, entity_id: str, result: dict[str, Any] | None
) -> NoReturn:
    """Report a failed registry read as itself: anything but Core's
    ``not_found`` proves nothing about whether the entity is registered."""
    failure = result or {}
    detail = failure.get("error") or "unknown error" if result else "no response"
    raise_failure(
        create_error_response(
            ws_failure_code(failure),
            (
                f"Reading the entity registry for {entity_id} failed: "
                f"{detail}. Nothing was deleted."
            ),
            context={"target": target, "entity_id": entity_id},
            # A proxy-blocked read carries its own guidance.
            suggestions=failure.get("suggestions")
            or [
                "Retry; check the Home Assistant connection if it keeps failing.",
            ],
        )
    )


class StorageRemovalTools(HelperStorageTools):
    def _classify_removal_error(
        self,
        error: Exception,
        context: dict[str, Any],
        *,
        suggestions: list[str] | None = None,
    ) -> NoReturn:
        raise_classified(error, context, suggestions=suggestions)

    async def _stored_remove_target(self, helper_type: str, target: str) -> Any:
        return await self.backend.execute(
            "helpers.remove.storage_target",
            {"helper_type": helper_type, "target": target},
            self.context,
        )

    async def _resolve_helper_unique_id(
        self, entity_id: str
    ) -> tuple[str | None, dict[str, Any] | None, bool]:
        result = await self.backend.execute(
            "helpers.remove.registry_target", {"entity_id": entity_id}, self.context
        )
        return result["unique_id"], result["registry_result"], result["component_used"]

    async def _state_absent(self, entity_id: str) -> bool:
        result = await self.backend.execute(
            "states.read", {"entity_ids": [entity_id]}, self.context
        )
        return entity_id not in result["states"]

    async def _registry_still_has_entry(self, target: str, entity_id: str) -> bool:
        result = await self.backend.execute(
            "helpers.remove.entity", {"entity_id": entity_id}, self.context
        )
        if result["entry"] is not None:
            return True
        if result["reason"] in {"not_in_registry", "not_registry_managed"}:
            return False
        raise ToolFailure("CONNECTION_FAILED", "The helper registry could not be read.")

    async def _wait_removed(self, entity_id: str) -> bool:
        return cast(
            bool,
            await self.backend.execute(
                "entities.removal.wait", {"entity_id": entity_id}, self.context
            ),
        )

    async def _verify_removed(self, entity_id: str, response: dict[str, Any]) -> None:
        from .removal_wait import verify_removed

        await verify_removed(self._wait_removed, entity_id, response)

    async def _remove_storage(
        self, helper_type: str, item_id: str, target: str, entity_id: str | None
    ) -> dict[str, Any]:
        async with registry_update_lock(f"helper_{helper_type}", item_id):
            try:
                current = await self.backend.execute(
                    "helpers.storage.read",
                    {"helper_type": helper_type, "item_id": item_id},
                    self.context,
                )
            except ToolFailure as error:
                if error.code != "ENTITY_NOT_FOUND":
                    raise
                current = {"success": False}
            if current.get("success") is not True:
                return {
                    "success": False,
                    "error_code": "not_found",
                    "error": "Stored item not found",
                }
            backup = await self.backend.execute(
                "helpers.backup.capture",
                {
                    "helper_type": helper_type,
                    "source_tool": "ha_remove_helpers_integrations",
                    "action": "remove",
                    "backup_id": item_id,
                    "item_id": item_id,
                    "entity_id": current.get("entity_id"),
                    "current_item": current["item"],
                },
                self.context,
            )
            result: dict[str, Any] = await self.backend.execute(
                "helpers.storage.remove",
                {
                    "helper_type": helper_type,
                    "item_id": item_id,
                    "expected_item": current["item"],
                    **(
                        {"expected_registry": backup["expected_registry"]}
                        if "expected_registry" in backup
                        else {}
                    ),
                },
                self.context,
            )
        if backup.get("warnings"):
            result.setdefault("warnings", []).extend(backup["warnings"])
        return result

    async def _delete_simple_helper(
        self,
        helper_type: str,
        target: str,
        wait_bool: bool,
    ) -> dict[str, Any]:
        """Delete a SIMPLE helper via the websocket {type}/delete API.

        A bare id is a storage id (``stored_helper_target``), as
        ``ha_config_set_helper`` and the backups resolve it, and an entity_id
        goes by its registry entry: one read through the component, or the
        3-retry lookup without it. A target still unresolved is classified by
        why the registry has no usable record.
        """
        try:
            stored = await self._stored_remove_target(helper_type, target)
            if stored is not None:
                storage_id, entity, reason = stored
                response = await self._delete_simple_via_unique_id(
                    helper_type, storage_id, target, entity, wait_bool
                )
                return removal_reply(response, entity, reason)
            # stored_helper_target refuses a bare id no stored helper has.
            entity_id = target
            (
                unique_id,
                registry_result,
                component_used,
            ) = await self._resolve_helper_unique_id(entity_id)

            if not unique_id:
                return await self._delete_simple_helper_fallback(
                    helper_type,
                    target,
                    entity_id,
                    registry_result,
                    component_used,
                )

            return await self._delete_simple_via_unique_id(
                helper_type, unique_id, target, entity_id, wait_bool
            )

        except ToolFailure:
            raise
        except Exception as e:  # noqa: BLE001
            self._classify_removal_error(
                e,
                context={"helper_type": helper_type, "target": target},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify target exists using ha_search()",
                    "Ensure helper is not used by automations or scripts",
                ],
            )
            return None  # unreachable: exception_to_structured_error raises

    async def _delete_simple_helper_fallback(
        self,
        helper_type: str,
        target: str,
        entity_id: str,
        registry_result: dict[str, Any] | None,
        component_used: bool,
    ) -> dict[str, Any]:
        """Classify a SIMPLE-helper target that names no stored helper.

        No stored id matched (``stored_helper_target``) and the registry
        lookup yielded no unique_id; deleting by the object id would guess a
        storage id that can belong to another helper's renamed entity. The
        target is absent from state and registry, or registered without a
        unique_id (ENTITY_NOT_FOUND); absent from state but still registered
        (SERVICE_CALL_FAILED); present but not registry-managed
        (RESOURCE_NOT_FOUND); or behind a failed registry read (its code).
        Always raises a structured error.
        """
        # Confirm via the registry too — a disabled entity is
        # state-absent but still registry-resident, so
        # state-absence alone is not enough to classify as
        # confirmed-absent. The APIError-404 branch routes the
        # never-existed-target case (HA returns 404 on
        # get_entity_state for unknown entity_ids) into the same
        # confirmed-absent path so the resulting ENTITY_NOT_FOUND
        # raise carries the structured "typo or removed" hint
        # message rather than a raw 404.
        if await self._state_absent(entity_id):
            if not await self._registry_still_has_entry(target, entity_id):
                logger.info(
                    f"Entity {entity_id} absent from state and "
                    "registry; surfacing as ENTITY_NOT_FOUND"
                )
                # Entity-shape target confirmed absent from both
                # the state machine and the entity registry.
                # Surface as ENTITY_NOT_FOUND — silent success
                # would mask the typo case (agent passed the
                # wrong helper_id / entity_id). Matches sibling
                # ha_remove_entity.
                raise_failure(
                    create_error_response(
                        ErrorCode.ENTITY_NOT_FOUND,
                        (
                            f"Helper {entity_id} not found. May indicate "
                            "it was already removed, never "
                            "existed, or the identifier is a "
                            "typo. Verify with "
                            "ha_search() before "
                            "retrying."
                        ),
                        context={
                            "target": target,
                            "helper_type": helper_type,
                            "entity_id": entity_id,
                        },
                    )
                )

            logger.warning(
                f"Entity {entity_id} absent from state but still "
                "in registry; treating as SERVICE_CALL_FAILED"
            )
            raise_failure(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    (
                        f"Helper {target} could not be deleted: "
                        "its registry entry exists, but no "
                        "unique_id to delete it by was read from it."
                    ),
                    suggestions=[
                        "Re-enable the entity via "
                        + "ha_set_entity(enabled=True), then retry "
                        + "deletion.",
                        "Or inspect the entity registry entry "
                        + "directly to confirm unique_id presence.",
                    ],
                    context={
                        "target": target,
                        "entity_id": entity_id,
                    },
                )
            )

        # The state is present (an absent state raised above). If the
        # registry definitely has no entry — a component miss, or Core's
        # not_found — the entity is not registry-managed rather than missing.
        registry_miss = not (registry_result or {}).get("success") and (
            component_used or (registry_result or {}).get("error_code") == "not_found"
        )
        if registry_miss:
            raise_unregistered_entity_error(target, helper_type, entity_id)
        if not (registry_result or {}).get("success"):
            # The state shows the entity exists; report the read failure.
            _raise_registry_read_failure(target, entity_id, registry_result)

        # The registry returned an entry, but without a unique_id. The
        # component path resolves via ONE authoritative in-process lookup (no
        # retry loop), so its text must not claim "3 attempts".
        if component_used:
            not_found_detail = (
                f"Component registry lookup found no unique_id for {entity_id}."
            )
        else:
            not_found_detail = (
                f"Registry entry for {entity_id} has no unique_id after 3 attempts."
            )
        raise_failure(
            create_error_response(
                ErrorCode.ENTITY_NOT_FOUND,
                not_found_detail,
                suggestions=[
                    "Helper may not be properly registered or was "
                    "already deleted. Use ha_search() to "
                    "verify.",
                ],
                context={"target": target, "entity_id": entity_id},
            )
        )
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    async def _delete_simple_via_unique_id(
        self,
        helper_type: str,
        unique_id: str,
        target: str,
        entity_id: str | None,
        wait_bool: bool,
    ) -> dict[str, Any]:
        """Delete a SIMPLE helper by its storage id; ``entity_id`` is None when
        the helper has no entity in the registry."""
        result = await self._remove_storage(helper_type, unique_id, target, entity_id)

        if result.get("success"):
            response: dict[str, Any] = {
                "success": True,
                "action": "delete",
                "target": target,
                "helper_type": helper_type,
                "method": "websocket_delete",
                "entry_id": None,
                "entity_ids": [entity_id] if entity_id else [],
                "require_restart": False,
                "unique_id": unique_id,
                "message": (
                    f"Successfully deleted {helper_type}: {target} "
                    f"(entity: {entity_id})."
                ),
            }
            if wait_bool and entity_id:
                await self._verify_removed(entity_id, response)
            if result.get("warnings"):
                response.setdefault("warnings", []).extend(result["warnings"])
            return response

        # Core's storage collection holds only UI-created items; a YAML helper
        # has a registry entry with a unique_id but no stored item to delete.
        if ws_failure_code(result) is ErrorCode.RESOURCE_NOT_FOUND:
            code, message, suggestion = not_stored_reply(helper_type, unique_id, target)
            raise_failure(
                create_error_response(
                    code,
                    message,
                    context={
                        "target": target,
                        "entity_id": entity_id,
                        "unique_id": unique_id,
                    },
                    suggestions=[suggestion],
                )
            )

        error_msg = result.get("error", "Unknown error")
        if isinstance(error_msg, dict):
            error_msg = error_msg.get("message", str(error_msg))
        raise_failure(
            create_error_response(
                ws_failure_code(result),
                f"Failed to delete helper: {error_msg}",
                suggestions=[
                    "Make sure the helper exists and is not being used "
                    "by automations or scripts",
                ],
                context={
                    "target": target,
                    "entity_id": entity_id,
                    "unique_id": unique_id,
                },
            )
        )
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable
