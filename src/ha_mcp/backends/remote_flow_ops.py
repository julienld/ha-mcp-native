"""Configuration-flow ports on the existing remote client."""

from typing import Any

from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation


class RemoteFlows:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.owned: dict[str, tuple[str, str | None, str]] = {}

    async def execute(
        self, operation: str, arguments: dict[str, Any], context: CallContext
    ) -> dict[str, Any]:
        if context.read_only:
            raise ToolFailure(
                "READ_ONLY",
                "Configuration flows can write state.",
                status="not_started",
            )
        if operation == "config.flow.start":
            if len(self.owned) >= 32:
                raise ToolFailure(
                    "FLOW_LIMIT",
                    "Too many active configuration flows.",
                    status="not_started",
                )
            kind = arguments.get("kind", "config")
            result: dict[str, Any] = await self._start(kind, arguments)
            self._record(result, kind, context)
            return result
        flow_id = arguments["flow_id"]
        owner = self.owned.get(flow_id)
        if owner is None or owner[1:] != (context.user_id, context.client_id):
            raise ToolFailure(
                "FLOW_NOT_OWNED",
                "This caller does not own this flow.",
                status="not_started",
            )
        kind = owner[0]
        prefixes = {
            "config": "config",
            "options": "options",
            "subentry": "config_subentry",
        }
        if operation == "config.flow.abort":
            result = await getattr(self.client, f"abort_{prefixes[kind]}_flow")(flow_id)
            self.owned.pop(flow_id, None)
            return result
        if operation != "config.flow.configure" or "user_input" not in arguments:
            raise UnsupportedOperation(operation)
        result = await getattr(self.client, f"submit_{prefixes[kind]}_flow_step")(
            flow_id, arguments["user_input"]
        )
        self._record(result, kind, context)
        return result

    async def _start(self, kind: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if kind == "config":
            if arguments.get("entry_id"):
                result: dict[str, Any] = await self.client.start_reconfigure_flow(
                    arguments["handler"], arguments["entry_id"]
                )
            else:
                result = await self.client.start_config_flow(arguments["handler"])
            return result
        if kind == "options":
            result = await self.client.start_options_flow(arguments["handler"])
            return result
        if kind == "subentry":
            entry_id, subentry_type = arguments["handler"]
            result = await self.client.start_config_subentry_flow(
                entry_id, subentry_type, subentry_id=arguments.get("subentry_id")
            )
            return result
        raise UnsupportedOperation(f"config.flow.kind.{kind}")

    def _record(self, result: dict[str, Any], kind: str, context: CallContext) -> None:
        flow_id = result.get("flow_id")
        if flow_id:
            if result.get("type") in {"create_entry", "abort"}:
                self.owned.pop(flow_id, None)
            else:
                self.owned[flow_id] = (kind, context.user_id, context.client_id)
