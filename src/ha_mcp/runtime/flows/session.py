"""Finite flow ports used by the existing walkers, without a network client."""

from typing import Any, cast

from ha_mcp.tool_api import Backend, BackendTools, CallContext


class FlowSession(BackendTools):
    """Config/options/subentry operations only; no URL, token, HTTP or WS API."""

    def __init__(
        self,
        backend: Backend,
        context: CallContext,
        *,
        helper_type: str | None = None,
        capture_backup: bool = False,
    ) -> None:
        super().__init__(backend, context)
        self.helper_type = helper_type
        self.capture_backup = capture_backup
        self.warnings: list[str] = []

    async def start_config_flow(self, handler: str) -> dict[str, Any]:
        return await self._mapping(
            "config.flow.start", {"kind": "config", "handler": handler}, self.context
        )

    async def start_options_flow(self, entry_id: str) -> dict[str, Any]:
        expected = await self._backup(entry_id)
        return await self._mapping(
            "config.flow.start",
            {"kind": "options", "handler": entry_id, **expected},
            self.context,
        )

    async def start_config_subentry_flow(
        self,
        entry_id: str,
        subentry_type: str,
        *,
        subentry_id: str | None = None,
        show_advanced_options: bool | None = None,
    ) -> dict[str, Any]:
        expected = (
            await self._backup(entry_id, subentry_id, subentry_type)
            if subentry_id
            else {}
        )
        return await self._mapping(
            "config.flow.start",
            {
                "kind": "subentry",
                "handler": [entry_id, subentry_type],
                "subentry_id": subentry_id,
                "show_advanced_options": show_advanced_options,
                **expected,
            },
            self.context,
        )

    async def submit_config_flow_step(
        self, flow_id: str, user_input: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._mapping(
            "config.flow.configure",
            {"flow_id": flow_id, "user_input": user_input},
            self.context,
        )

    submit_options_flow_step = submit_config_flow_step
    submit_config_subentry_flow_step = submit_config_flow_step

    async def abort_config_flow(self, flow_id: str) -> dict[str, Any]:
        return await self._mapping(
            "config.flow.abort", {"flow_id": flow_id}, self.context
        )

    abort_options_flow = abort_config_flow
    abort_config_subentry_flow = abort_config_flow

    async def get_config_entry(self, entry_id: str) -> dict[str, Any]:
        return await self._mapping(
            "config.entries.read", {"entry_id": entry_id}, self.context
        )

    async def list_config_subentries(self, entry_id: str) -> dict[str, Any]:
        rows = await self.backend.execute(
            "config.subentries.list", {"entry_id": entry_id}, self.context
        )
        return {"success": True, "result": rows}

    async def _backup(
        self,
        entry_id: str,
        subentry_id: str | None = None,
        subentry_type: str | None = None,
    ) -> dict[str, Any]:
        if not self.capture_backup:
            return {}
        current = await self.get_config_entry(entry_id)
        result = await self.backend.execute(
            "helpers.backup.capture",
            {
                "kind": "subentry" if subentry_id else "flow",
                "helper_type": self.helper_type,
                "action": "update",
                "backup_id": f"{entry_id}/{subentry_id}" if subentry_id else entry_id,
                "entry_id": entry_id,
                **(
                    {"subentry_id": subentry_id, "subentry_type": subentry_type}
                    if subentry_id
                    else {}
                ),
            },
            self.context,
        )
        self.warnings.extend(result.get("warnings", []))
        return (
            {"expected_modified_at": current["modified_at"]}
            if "modified_at" in current
            else {}
        )

    async def _mapping(
        self, operation: str, arguments: dict[str, Any], context: CallContext
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any], await self.backend.execute(operation, arguments, context)
        )
