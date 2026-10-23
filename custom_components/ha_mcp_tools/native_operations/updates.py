"""Native update management binds public planning to actual Core owners."""

from dataclasses import dataclass
from typing import Any

from ha_mcp.runtime.update_plans import single_update_id
from ha_mcp.tool_api import CallContext, ToolFailure

from .registry_access import registry_failure
from .update_writes import UpdateWrites


@dataclass(slots=True)
class NativeUpdates(UpdateWrites):
    manager: Any
    actor: CallContext

    async def manage(
        self,
        action: str = "list",
        entity_ids: list[str] | str | None = None,
        categories: list[str] | str | None = None,
        include_skipped: bool = False,
        include_release_notes: bool = False,
        backup: bool = False,
        repairs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        try:
            await self._authorize()
            if action == "list":
                return await self._list(include_skipped)
            if action == "get":
                return await self._get(
                    single_update_id(entity_ids), include_release_notes
                )
            if action in ("install", "skip", "clear_skipped"):
                return await self._updates(action, entity_ids, categories, backup)
            if action in ("ignore_repair", "unignore_repair"):
                return await self._repairs(action, repairs)
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                f"Invalid action for update management: {action}",
                status="not_started",
            )
        except Exception as error:
            raise registry_failure(error, {"action": action}) from error
