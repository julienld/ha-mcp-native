"""Explicit remote operations for the shared flow restoration algorithm."""

from typing import Any

from ha_mcp.backup_entity_ids import _manager
from ha_mcp.runtime.snapshot_flow_restore import _flow_recreation_preflight


class RemoteSnapshotFlowPorts:
    def __init__(self, client: Any) -> None:
        self.session = client
        self.errors = _manager()._CAPTURE_TRANSIENT_ERRORS

    async def helper(self, entry_id: str, helper_type: str) -> Any:
        return await _manager()._fetch_flow_helper(self.session, entry_id, helper_type)

    async def subentry(self, target: str) -> Any:
        return await _manager()._fetch_config_subentry(self.session, target)

    async def subentries(self, entry_id: str) -> Any:
        return await _manager()._list_config_subentries(self.session, entry_id)

    async def entries(self) -> Any:
        return await self.session._request("GET", "/config/config_entries/entry")

    async def preflight(
        self, entry_id: str, snapshot: dict[str, Any], helper_type: str
    ) -> Any:
        return await _flow_recreation_preflight(self, entry_id, snapshot, helper_type)

    async def restore_ids(
        self, entry_id: str, old_entry: str, rows: list[dict[str, Any]]
    ) -> Any:
        return await _manager()._restore_entity_ids(
            self.session, entry_id, old_entry, rows
        )

    async def check_collision(self, entity_id: str) -> None:
        await _manager()._check_entity_collision(self.session, entity_id)

    def log_failure(self, step: str, error: BaseException) -> None:
        _manager()._log_flow_helper_failure(step, error)

    async def created(self, entry_id: str, unique_id: str) -> Any:
        return await _manager()._created_entity(self.session, entry_id, unique_id)

    async def collision(
        self, target: str, *, owned_entry_id: str | None = None
    ) -> None:
        await _manager()._check_entity_collision(
            self.session, target, owned_entry_id=owned_entry_id
        )

    async def rename(
        self,
        entry_id: str,
        unique_id: str,
        changes: dict[str, Any],
        expected: dict[str, Any],
    ) -> Any:
        return await _manager()._ws_send(
            self.session, {"type": "config/entity_registry/update", **changes}
        )
