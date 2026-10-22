"""Shared config-entry and subentry removal, backup and completion policy."""

import asyncio
import logging
from contextlib import AbstractAsyncContextManager
from typing import Any, cast

from ha_mcp.errors import (
    ErrorCode,
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
    create_error_response,
)
from ha_mcp.tool_api import ToolFailure
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .errors import raise_failure, websocket_error_message
from .flows.entry import refuse_component_entry
from .flows.session import FlowSession
from .helper_removal_storage import StorageRemovalTools
from .helper_targets import get_entry_id_for_flow_helper, raise_flow_helper_lookup_error

logger = logging.getLogger(__name__)


class EntryRemovalTools(StorageRemovalTools):
    def _entry_guard(self, entry_id: str) -> AbstractAsyncContextManager[Any]:
        return registry_update_lock("config_entry", entry_id)

    async def _entry_entities(
        self, entry_id: str, warnings: list[str]
    ) -> list[dict[str, Any]]:
        result = await self.backend.execute(
            "helpers.entities.for_entry", {"config_entry_id": entry_id}, self.context
        )
        warnings.extend(result.get("warnings", []))
        return cast(list[dict[str, Any]], result["entities"])

    async def _read_remove_entry(self, entry_id: str) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self.backend.execute(
                "config.entries.read", {"entry_id": entry_id}, self.context
            ),
        )

    async def _remove_entry(
        self, entry_id: str, current: dict[str, Any]
    ) -> dict[str, Any]:
        kinds = await self.backend.execute(
            "helpers.flow.types",
            {"helper_type": current.get("domain", "")},
            self.context,
        )
        helper_type = (
            current.get("domain")
            if current.get("domain") in kinds["types"]
            else "integration"
        )
        return await self._capture_and_remove_entry(entry_id, str(helper_type), current)

    async def _capture_and_remove_entry(
        self, entry_id: str, helper_type: str, expected: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        from ha_mcp.utils.config_hash import json_values_equal

        async with self._entry_guard(entry_id):
            current = await self._read_remove_entry(entry_id)
            refuse_component_entry(
                current.get("domain"),
                action="delete a config entry of",
                entry_id=entry_id,
            )
            if expected is not None and not json_values_equal(expected, current):
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "The integration changed before its removal snapshot.",
                    status="not_started",
                )
            backup = await self.backend.execute(
                "helpers.backup.capture",
                {
                    "kind": "integration" if helper_type == "integration" else "flow",
                    "helper_type": helper_type,
                    "action": "remove",
                    "entry_id": entry_id,
                    "backup_domain": "integration"
                    if helper_type == "integration"
                    else f"helper_{helper_type}",
                    "source_tool": "ha_remove_helpers_integrations",
                    "backup_id": entry_id,
                    "current_entry": current,
                },
                self.context,
            )
            result: dict[str, Any] = await self.backend.execute(
                "config.entries.remove",
                {
                    "entry_id": entry_id,
                    "expected_entry": current,
                },
                self.context,
            )
        if backup.get("warnings"):
            result.setdefault("warnings", []).extend(backup["warnings"])
        return result

    async def _remove_subentry(self, entry_id: str, subentry_id: str) -> dict[str, Any]:
        async with self._entry_guard(entry_id):
            current = await self._read_remove_entry(entry_id)
            refuse_component_entry(
                current.get("domain"),
                action="delete a config subentry of",
                entry_id=entry_id,
            )
            rows = await self.backend.execute(
                "config.subentries.list", {"entry_id": entry_id}, self.context
            )
            row = next((row for row in rows if row["subentry_id"] == subentry_id), None)
            if row is None:
                return {
                    "success": False,
                    "error": {
                        "code": "not_found",
                        "message": "Config subentry not found",
                    },
                }
            backup = await self.backend.execute(
                "helpers.backup.capture",
                {
                    "kind": "subentry",
                    "helper_type": "config_subentry",
                    "action": "remove",
                    "entry_id": entry_id,
                    "subentry_id": subentry_id,
                    "subentry_type": row["subentry_type"],
                    "backup_id": f"{entry_id}/{subentry_id}",
                    "source_tool": "ha_remove_helpers_integrations",
                },
                self.context,
            )
            result: dict[str, Any] = await self.backend.execute(
                "config.subentries.remove",
                {
                    "entry_id": entry_id,
                    "subentry_id": subentry_id,
                    "expected_entry": current,
                },
                self.context,
            )
        if backup.get("warnings"):
            result.setdefault("warnings", []).extend(backup["warnings"])
        return result

    async def _delete_flow_helper(
        self,
        helper_type: str,
        target: str,
        wait_bool: bool,
        warnings: list[str],
    ) -> dict[str, Any]:
        """Resolve target entity_id to config_entry_id, then delete entry.

        Multi-entity helpers (e.g. utility_meter with tariffs) are handled
        naturally — any sub-entity resolves to the same entry_id, and all
        sub-entities are waited for in parallel via asyncio.gather.
        """
        client = FlowSession(self.backend, self.context)
        try:
            # Step 1: resolve target → entry_id (typed reason on failure)
            entry_id, reason = await get_entry_id_for_flow_helper(
                client, helper_type, target, warnings
            )
            if entry_id is None:
                raise_flow_helper_lookup_error(
                    reason, helper_type, target, detail="; ".join(warnings) or None
                )

            result: dict[str, Any] = await self._delete_resolved_flow_helper(
                helper_type=helper_type,
                target=target,
                entry_id=entry_id,
                wait_bool=wait_bool,
                warnings=warnings,
            )
            return result

        except ToolFailure:
            raise
        except Exception as e:  # noqa: BLE001
            self._classify_removal_error(
                e,
                context={
                    "helper_type": helper_type,
                    "target": target,
                },
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify the target exists using ha_search() "
                    + "or ha_get_integration()",
                ],
            )
            return None  # unreachable: exception_to_structured_error raises

    async def _delete_resolved_flow_helper(
        self,
        *,
        helper_type: str,
        target: str,
        entry_id: str,
        wait_bool: bool,
        warnings: list[str],
    ) -> dict[str, Any]:
        """Remove a resolved flow entry, capturing Templates under their write lock."""
        # Step 2: collect sub-entity IDs for the wait phase
        sub_entities = await self._entry_entities(entry_id, warnings)
        entity_ids = [e["entity_id"] for e in sub_entities if "entity_id" in e]

        # Step 3: delete the config entry
        delete_result = await self._delete_flow_config_entry(
            entry_id, target, helper_type
        )

        require_restart = bool(
            isinstance(delete_result, dict)
            and delete_result.get("require_restart", False)
        )

        # Step 4: wait for all sub-entities to be removed in parallel
        response: dict[str, Any] = {
            "success": True,
            "action": "delete",
            "target": target,
            "helper_type": helper_type,
            "method": "config_flow_delete",
            "entry_id": entry_id,
            "entity_ids": entity_ids,
            "require_restart": require_restart,
            "message": (
                f"Successfully deleted {helper_type} (entry: {entry_id}, "
                f"{len(entity_ids)} sub-entities)."
            ),
        }
        if wait_bool and entity_ids:
            results = await asyncio.gather(
                *[self._wait_removed(eid) for eid in entity_ids],
                return_exceptions=True,
            )
            # The entry is already gone: a failed check is a warning, as in the
            # sibling delete tools, never an error for a delete that worked.
            checks = dict(zip(entity_ids, results, strict=True))
            failed = {
                eid: res
                for eid, res in checks.items()
                if isinstance(res, BaseException)
            }
            for eid, exc in failed.items():
                if not isinstance(
                    exc, (HomeAssistantConnectionError, HomeAssistantAuthError)
                ):
                    logger.warning(
                        f"Unexpected error verifying removal of {eid}: {exc}",
                        exc_info=exc,
                    )
            if failed:
                response.setdefault("warnings", []).append(
                    "Deletion confirmed but removal verification failed: "
                    + "; ".join(f"{eid}: {exc}" for eid, exc in sorted(failed.items()))
                )
            not_removed = [
                eid
                for eid, res in checks.items()
                if res is not True and eid not in failed
            ]
            if not_removed:
                response.setdefault("warnings", []).append(
                    f"Deletion confirmed but the following entities "
                    f"are still present after the wait window: "
                    f"{not_removed}"
                )
        warnings.extend(delete_result.get("warnings", []))
        if warnings:
            response.setdefault("warnings", []).extend(warnings)
        return response

    async def _delete_flow_config_entry(
        self, entry_id: str, target: str, helper_type: str
    ) -> Any:
        """Delete the resolved config entry, mapping a delete-time 404 to NOT_FOUND.

        TOCTOU window: entry_id resolved at step 1 may be gone before the DELETE
        reaches HA. A 404 surfaces as RESOURCE_NOT_FOUND so a concurrent removal
        is not masked as success; non-404 errors bubble through
        exception_to_structured_error.
        """
        try:
            return await self._capture_and_remove_entry(entry_id, helper_type)
        except HomeAssistantAPIError as e:
            if e.status_code == 404:
                raise_failure(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        (
                            f"Config entry {entry_id} for {target} "
                            "not found at delete time (resolved by "
                            "registry but absent when DELETE reached "
                            "Home Assistant). May indicate a "
                            "concurrent removal."
                        ),
                        context={
                            "entry_id": entry_id,
                            "target": target,
                            "helper_type": helper_type,
                        },
                    )
                )
            self._classify_removal_error(
                e,
                context={
                    "entry_id": entry_id,
                    "target": target,
                    "helper_type": helper_type,
                },
            )
        except Exception as e:  # noqa: BLE001
            self._classify_removal_error(
                e,
                context={
                    "entry_id": entry_id,
                    "target": target,
                    "helper_type": helper_type,
                },
            )
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    async def _delete_config_subentry(
        self, entry_id: str, subentry_id: str
    ) -> dict[str, Any]:
        """Delete one config subentry under a parent config entry."""
        try:
            result = await self._remove_subentry(entry_id, subentry_id)
        except ToolFailure:
            raise
        except HomeAssistantAPIError as exc:
            if exc.status_code != 404:
                self._classify_removal_error(
                    exc, {"entry_id": entry_id, "subentry_id": subentry_id}
                )
            result = {"success": False, "error": {"code": "not_found"}}
        except Exception as exc:  # noqa: BLE001 - preserve the public facade classifier
            self._classify_removal_error(
                exc, {"entry_id": entry_id, "subentry_id": subentry_id}
            )
        if not isinstance(result, dict) or not result.get("success"):
            error = result.get("error", "Operation failed")
            error_msg = websocket_error_message(error)
            # Detect "subentry already absent" by HA's structured
            # ``code="not_found"`` only. A generic ``"not found" in
            # error_msg`` substring match was rejected because it can
            # collide with unrelated HA error messages (e.g.
            # "repository not found", "integration not found") and
            # mis-classify a real failure as a missing target.
            # The HA ``config_entries/subentries/delete`` handler raises
            # with ``code="not_found"`` when entry_id or subentry_id is
            # missing; if a future HA version uses a different code, we
            # raise SERVICE_CALL_FAILED instead — safer than mis-classifying.
            error_code = error.get("code") if isinstance(error, dict) else None
            if error_code == "not_found":
                # Subentry absent under the parent config entry. Surface
                # as RESOURCE_NOT_FOUND so the caller learns the target
                # didn't exist — silent success would mask a typo'd
                # subentry_id (or wrong parent entry_id) until the user
                # noticed nothing was removed.
                raise_failure(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        (
                            f"Subentry {subentry_id} not found under "
                            f"config entry {entry_id}. May indicate it "
                            "was already removed, never existed, or one "
                            "of the identifiers is a typo. Verify with "
                            "ha_get_integration(entry_id=..., "
                            "include_subentries=True) before retrying."
                        ),
                        context={
                            "entry_id": entry_id,
                            "subentry_id": subentry_id,
                        },
                    )
                )
            raise_failure(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to delete config subentry: {error_msg}",
                    context={"entry_id": entry_id, "subentry_id": subentry_id},
                )
            )
        return {
            "success": True,
            "action": "delete",
            "target": entry_id,
            "helper_type": "config_subentry",
            "subentry_id": subentry_id,
            **({"warnings": result["warnings"]} if result.get("warnings") else {}),
            "method": "config_subentry_delete",
            "message": f"Successfully deleted config subentry: {subentry_id}",
        }
