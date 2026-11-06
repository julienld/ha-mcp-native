"""Saved raw API spellings adapted to bounded native owners and real tools."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

from ha_mcp.runtime.code_api_policy import _normalize_endpoint, api_post_policy_refusal
from ha_mcp.runtime.websocket_policy import raw_ws_refusal, validate_ws_request
from ha_mcp.tool_api import ToolFailure, UnsupportedOperation

from .native_raw_commands import RAW_COMMANDS


class NativeCodeAPI:
    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge

    async def api_get(self, endpoint: Any) -> Any:
        if error := self.bridge._limit():
            return error
        try:
            path = _normalize_endpoint(endpoint)
            name, arguments = _get_route(path)
            return await self.bridge._invoke_child(
                name,
                arguments,
                operation="code.api.get",
                payload={"path": path},
                raw=True,
            )
        except ToolFailure as failure:
            return failure.data
        except ValueError as failure:
            return {"error": str(failure)}

    async def api_post(self, endpoint: Any, data: Any = None) -> Any:
        if error := self.bridge._limit():
            return error
        try:
            path = _normalize_endpoint(endpoint)
            if refusal := api_post_policy_refusal(path):
                return {"error": refusal}
            if path == "template":
                return await self.bridge._invoke_child(
                    "ha_eval_template",
                    data if isinstance(data, dict) else {},
                    operation="code.api.template",
                    payload={"data": data},
                    raw=True,
                )
            if path.startswith("services/"):
                return await self._service(path, data)
            if (
                path.startswith("events/")
                and path.count("/") == 1
                and "?" not in path
                and "#" not in path
                and "%" not in path
            ):
                return await self.bridge._invoke_child(
                    "ha_call_event",
                    {"event_type": path.removeprefix("events/"), "data": data},
                    raw=True,
                )
            raise UnsupportedOperation(
                f"native API POST /{path}; use call_tool or the legacy runtime"
            )
        except ToolFailure as failure:
            return failure.data
        except ValueError as failure:
            return {"error": str(failure)}

    async def _service(self, path: str, data: Any) -> Any:
        parsed = urlsplit(path)
        parts = parsed.path.split("/")
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (
            len(parts) != 3
            or not all(parts)
            or set(query) - {"return_response"}
            or parsed.fragment
            or "%" in parsed.path
        ):
            raise UnsupportedOperation(f"native API POST /{path}")
        response_requested = "return_response" in query
        result = await self.bridge._invoke_child(
            "ha_call_service",
            {
                "domain": parts[1],
                "service": parts[2],
                "data": data,
                "wait": False,
                "verbose": True,
                "return_response": response_requested,
            },
            raw=True,
        )
        if not isinstance(result, dict) or "error" in result:
            return result
        if response_requested:
            return {
                "changed_states": result["result"],
                "service_response": result.get("service_response"),
            }
        return result["result"]

    async def ws_send(self, message: Any) -> Any:
        if error := self.bridge._limit():
            return error
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            return {"error": "ws_send(message) requires a dict with a 'type' field"}
        command = message["type"]
        payload = {key: value for key, value in message.items() if key != "type"}
        try:
            if refusal := raw_ws_refusal(command, payload, sandbox=True):
                return {"error": refusal.as_text()}
            command, payload = validate_ws_request(
                command, payload, domain=None, service=None
            )
            if command not in RAW_COMMANDS:
                raise UnsupportedOperation(
                    f"native command {command}; use call_tool or the legacy runtime"
                )
            result = await self.bridge._invoke_child(
                "ha_call_service",
                {"ws_command": command, "data": payload or None},
                raw=True,
            )
            return result.get("result", result) if isinstance(result, dict) else result
        except ToolFailure as failure:
            return failure.data


def _get_route(path: str) -> tuple[str, dict[str, Any]]:
    if path == "config":
        return "ha_get_system_health", {"native_api_path": "/config"}
    if path == "states":
        return "ha_get_state", {"native_api_path": "/states"}
    if (
        path.startswith("states/")
        and path.count("/") == 1
        and "?" not in path
        and "#" not in path
    ):
        return "ha_get_state", {"entity_id": path.removeprefix("states/")}
    if path == "services":
        return "ha_list_services", {}
    raise UnsupportedOperation(
        f"native API GET /{path}; use call_tool or the legacy runtime"
    )
