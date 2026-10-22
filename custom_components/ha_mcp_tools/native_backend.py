"""Native semantic ports on the HA loop, with no local HTTP/WS transport."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import voluptuous as vol
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.json import json_bytes

from ha_mcp.runtime.service_policy import check_service_policy
from ha_mcp.runtime.skill_files import read_skill_files
from ha_mcp.runtime.skills import SkillGuideReader
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation

from .native_registry_ops import COMMAND_OPERATIONS as REGISTRY_COMMAND_OPERATIONS
from .native_registry_ops import DIRECT_OPERATIONS as REGISTRY_DIRECT_OPERATIONS

# A capability boundary, not an emulation of HomeAssistantClient. Each row reuses
# an owned, tested operation behind the existing component transport facade.
COMMAND_OPERATIONS: dict[str, tuple[str, bool]] = {
    **REGISTRY_COMMAND_OPERATIONS,
    "states.read": ("ha_mcp_tools/states", False),
    "services.catalog": ("ha_mcp_tools/services_list", False),
    "services.call_observe": ("ha_mcp_tools/call_service", True),
    "helpers.storage.schemas": ("ha_mcp_tools/helper_schemas", False),
    "helpers.storage.read": ("ha_mcp_tools/helper_item", False),
    "helpers.storage.write": ("ha_mcp_tools/helper_write", True),
}


class NativeBackend:
    """Core imports stay here; tool orchestration is shared with remote callers."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        service_policy: Callable[[str, str], None] | None = None,
        skill_guide_reader: SkillGuideReader | None = None,
        skill_content_enabled: Callable[[], bool] | None = None,
    ) -> None:
        from .websocket_api import _command_specs

        self.hass = hass
        self.service_policy = service_policy
        self.skill_guide_reader = skill_guide_reader or SkillGuideReader()
        self.skill_content_enabled = skill_content_enabled or (lambda: True)
        self.loop = asyncio.get_running_loop()
        self._commands: dict[str, tuple[Any, Any, Any]] = {}
        self._flows: Any = None
        selected = {command for command, _ in COMMAND_OPERATIONS.values()}
        for schema, do, prep in _command_specs():
            command = next(value for key, value in schema.items() if str(key) == "type")
            if command in selected:
                self._commands[command] = (vol.Schema(schema), do, prep)

    async def authorize(self, context: CallContext) -> None:
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("Native operations must run on the HA event loop")
        if context.credential is not None:
            await context.credential()
        user = (
            await self.hass.auth.async_get_user(context.user_id)
            if context.user_id
            else None
        )
        if (
            user is None
            or not user.is_active
            or not user.is_admin
            or getattr(user, "system_generated", False)
        ):
            raise PermissionError(
                "An active human Home Assistant administrator is required"
            )

    async def execute(
        self, operation: str, arguments: dict[str, Any], context: CallContext
    ) -> Any:
        await self.authorize(context)
        if {"type", "id", "ha_mcp_context"}.intersection(arguments):
            raise ToolFailure(
                "INVALID_ARGUMENTS",
                "Protected operation envelope field.",
                status="not_started",
            )
        if operation in REGISTRY_DIRECT_OPERATIONS:
            result = await REGISTRY_DIRECT_OPERATIONS[operation](self.hass, arguments)
            return json.loads(json_bytes(result))
        if operation == "skills.guide":
            return await self.hass.async_add_executor_job(
                self.skill_guide_reader.read, arguments.get("file", "SKILL.md")
            )
        if operation == "skills.files":
            enabled = self.skill_content_enabled()
            return await self.hass.async_add_executor_job(
                lambda: read_skill_files(arguments["references"], enabled=enabled)
            )
        if operation in {
            "config.flow.start",
            "config.flow.configure",
            "config.flow.abort",
        }:
            from .native_operations.flows import NativeFlows

            if self._flows is None:
                self._flows = NativeFlows(self.hass, self.authorize)
            result = await self._flows.execute(operation, arguments, context)
            return json.loads(json_bytes(result))
        if operation == "instance.timezone":
            return {"timezone": self.hass.config.time_zone, "fetch_failed": False}
        if operation == "services.policy":
            self._check_service_policy(arguments["domain"], arguments["service"])
            return None
        return await self._execute_command(operation, arguments, context)

    async def _execute_command(
        self, operation: str, arguments: dict[str, Any], context: CallContext
    ) -> Any:
        if operation not in COMMAND_OPERATIONS:
            raise UnsupportedOperation(operation)
        command, writes = COMMAND_OPERATIONS[operation]
        if writes and context.read_only:
            raise ToolFailure("READ_ONLY", "Writes are disabled for this caller.")
        schema, do, prep = self._commands[command]
        payload = dict(arguments)
        include_changed_states = (
            payload.pop("include_changed_states", False)
            if operation == "services.call_observe"
            else False
        )
        message = schema({"type": command, **payload})
        message["ha_mcp_context"] = Context(user_id=context.user_id)
        if operation == "services.call_observe":
            from .native_operations.services import call_and_observe

            self._check_service_policy(message["domain"], message["service"])
            result = await call_and_observe(
                self.hass,
                message,
                prep,
                do,
                include_changed_states=include_changed_states,
            )
            return json.loads(json_bytes(result))
        extra = await prep(self.hass, message) if prep is not None else {}
        result = do(self.hass, message, **extra)
        # Same HA encoder as the former wire path; datetime/enum values remain
        # JSON-compatible before reaching either MCP or Assist.
        return json.loads(json_bytes(result))

    def _check_service_policy(self, domain: str, service: str) -> None:
        if self.service_policy is not None:
            self.service_policy(domain, service)
            return
        check_service_policy(domain, service)

    async def stop(self) -> None:
        """Called after engine drain; release only resources this backend owns."""
        if self._flows is not None:
            await self._flows.stop()
