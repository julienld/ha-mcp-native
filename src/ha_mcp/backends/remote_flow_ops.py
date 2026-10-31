"""Configuration-flow ports on the existing remote client."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.runtime.errors import raise_failure, websocket_error_message
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation


async def read_flow_operation(
    client: Any, operation: str, arguments: dict[str, Any]
) -> Any:
    if operation == "config.entries.read":
        return await client.get_config_entry(arguments["entry_id"])
    if operation == "config.subentries.list":
        response = await client.list_config_subentries(arguments["entry_id"])
        if not isinstance(response, dict) or response.get("success") is not True:
            error = websocket_error_message(
                response.get("error", "Operation failed")
                if isinstance(response, dict)
                else "Operation failed"
            )
            raise_failure(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to list config subentries: {error}",
                    context={"entry_id": arguments["entry_id"]},
                )
            )
        if not isinstance(response.get("result"), list):
            raise_failure(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Unexpected config subentry list response",
                    context={"entry_id": arguments["entry_id"], "details": response},
                )
            )
        return response["result"]
    if operation == "config.flow.settings":
        from ha_mcp.redaction import redaction_enabled

        return {"redact_secrets": redaction_enabled()}
    if operation == "helpers.flow.types":
        from ha_mcp.tools.helper_flows import listed_helper_flows

        return {
            "types": sorted(await listed_helper_flows(client, arguments["helper_type"]))
        }
    raise UnsupportedOperation(operation)


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
            expected = arguments.get("expected_modified_at")
            if expected is not None:
                entry_id = (
                    arguments["handler"]
                    if kind == "options"
                    else arguments["handler"][0]
                    if kind == "subentry"
                    else arguments.get("entry_id")
                )
                if entry_id is None:
                    raise UnsupportedOperation(
                        "config.flow.expected_revision_requires_entry"
                    )
                current = await self.client.get_config_entry(entry_id)
                if current.get("modified_at") != expected:
                    raise ToolFailure(
                        "CONFIG_CONFLICT",
                        "The config entry changed after its snapshot was read.",
                        status="not_started",
                    )
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
                entry_id,
                subentry_type,
                subentry_id=arguments.get("subentry_id"),
                show_advanced_options=arguments.get("show_advanced_options"),
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
