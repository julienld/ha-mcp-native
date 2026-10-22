"""Semantic ports backed by the existing, explicitly remote HA clients."""

from __future__ import annotations

import asyncio
from typing import Any

from ha_mcp.tool_api import CallContext, UnsupportedOperation


class RemoteBackend:
    """Reuse current transport ownership; never invent a client inside HA."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self._flows: Any = None
        self._reload_watches: Any = None
        self._entry_watches: Any = None

    async def authorize(self, context: CallContext) -> None:
        # Remote authorization is enforced by HA on every wire request. The
        # entrypoint may additionally attach its live OAuth/session validator.
        if context.credential is not None:
            await context.credential()

    async def execute(
        self, operation: str, arguments: dict[str, Any], context: CallContext
    ) -> Any:
        await self.authorize(context)
        if operation in {
            "config.entries.read",
            "config.entries.watch",
            "config.entries.observe",
            "config.entries.close",
            "config.subentries.list",
            "config.flow.settings",
            "helpers.flow.types",
            "config.reload.watch",
            "config.reload.wait",
            "config.reload.close",
            "config.flow.start",
            "config.flow.configure",
            "config.flow.abort",
        }:
            return await self._flow_operation(operation, arguments, context)
        if operation in {"skills.guide", "skills.files"}:
            from .remote_skill_ops import execute_skill_operation

            return await execute_skill_operation(operation, arguments)
        if operation == "instance.timezone":
            from ha_mcp.tools.response_helpers import fetch_ha_timezone

            timezone, failed = await fetch_ha_timezone(self.client)
            return {"timezone": timezone, "fetch_failed": failed}
        if operation == "services.catalog":
            from ha_mcp.tools.tools_services import _fetch_catalog_and_translations

            warnings: list[str] = []
            services, translations = await _fetch_catalog_and_translations(
                self.client, arguments.get("domain"), warnings
            )
            return {
                "services": services,
                "translations": translations,
                "warnings": warnings,
            }
        if operation == "states.read":
            return await self._states(arguments["entity_ids"])
        return await self._domain_operation(operation, arguments)

    async def _domain_operation(self, operation: str, arguments: dict[str, Any]) -> Any:
        from .remote_integration_ops import (
            INTEGRATION_OPERATIONS,
            execute_integration_operation,
        )

        if operation in INTEGRATION_OPERATIONS:
            return await execute_integration_operation(
                self.client, operation, arguments
            )
        from .remote_todo_ops import TODO_OPERATIONS, execute_todo_operation

        if operation in TODO_OPERATIONS:
            return await execute_todo_operation(self.client, operation, arguments)
        from .remote_registry_ops import REGISTRY_OPERATIONS, execute_registry_operation

        if operation in REGISTRY_OPERATIONS:
            return await execute_registry_operation(self.client, operation, arguments)
        from .remote_service_ops import SERVICE_OPERATIONS, execute_service_operation

        if operation in SERVICE_OPERATIONS:
            return await execute_service_operation(self.client, operation, arguments)
        from .remote_helper_ops import HELPER_OPERATIONS, execute_helper_operation

        if operation in HELPER_OPERATIONS:
            return await execute_helper_operation(self.client, operation, arguments)
        raise UnsupportedOperation(operation)

    async def _flow_operation(
        self, operation: str, arguments: dict[str, Any], context: CallContext
    ) -> Any:
        from .remote_flow_ops import RemoteFlows, read_flow_operation

        if operation in {
            "config.entries.watch",
            "config.entries.observe",
            "config.entries.close",
        }:
            from .remote_entry_watch import RemoteEntryWatches

            if self._entry_watches is None:
                self._entry_watches = RemoteEntryWatches(self.client)
            return await self._entry_watches.execute(operation, arguments, context)
        if operation in {
            "config.reload.watch",
            "config.reload.wait",
            "config.reload.close",
        }:
            from .remote_reload import RemoteReloadWatches

            if self._reload_watches is None:
                self._reload_watches = RemoteReloadWatches(self.client)
            return await self._reload_watches.execute(operation, arguments, context)
        if operation in {
            "config.flow.start",
            "config.flow.configure",
            "config.flow.abort",
        }:
            if self._flows is None:
                self._flows = RemoteFlows(self.client)
            return await self._flows.execute(operation, arguments, context)
        return await read_flow_operation(self.client, operation, arguments)

    async def stop(self) -> None:
        if self._reload_watches is not None:
            await self._reload_watches.stop()
        if self._entry_watches is not None:
            await self._entry_watches.stop()

    async def _states(self, entity_ids: list[str]) -> dict[str, Any]:
        from ha_mcp.tools.search.state import StateMixin

        # The old reader preserves component negotiation and REST fallback for
        # read-only operations. It is never imported by the native entrypoint.
        reader = StateMixin.__new__(StateMixin)
        reader._client = self.client
        component = await reader._fetch_states_via_component(entity_ids)
        if component is not None:
            return component
        results = await asyncio.gather(
            *(reader._fetch_single_state(eid) for eid in entity_ids)
        )
        states = {
            row["entity_id"]: row["state"]
            for row in results
            if row.get("success") is True
        }
        return {
            "states": states,
            "missing": [eid for eid in entity_ids if eid not in states],
            "errors": {
                eid: row
                for eid, row in zip(entity_ids, results, strict=True)
                if not row.get("success")
            },
        }
