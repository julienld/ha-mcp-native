"""Native semantic ports on the HA loop, with no local HTTP/WS transport."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import voluptuous as vol
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.json import json_bytes

from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.runtime.service_policy import check_service_policy
from ha_mcp.runtime.skill_files import read_skill_files
from ha_mcp.runtime.skills import SkillGuideReader
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation

from .native_operations.area_floor_mutations import (
    DIRECT_OPERATIONS as AREA_FLOOR_OPERATIONS,
)
from .native_operations.config_finalize import (
    DIRECT_OPERATIONS as CONFIG_FINALIZE_OPERATIONS,
)
from .native_operations.flow_records import DIRECT_OPERATIONS as FLOW_DIRECT_OPERATIONS
from .native_operations.helper_listing import (
    DIRECT_OPERATIONS as LISTING_DIRECT_OPERATIONS,
)
from .native_operations.helpers import DIRECT_OPERATIONS as HELPER_DIRECT_OPERATIONS
from .native_operations.integration_inspection import (
    DIRECT_OPERATIONS as INTEGRATION_INSPECTION_OPERATIONS,
)
from .native_operations.integration_reads import (
    DIRECT_OPERATIONS as INTEGRATION_READ_OPERATIONS,
)
from .native_operations.registry_mutations import (
    DIRECT_OPERATIONS as REGISTRY_MUTATION_OPERATIONS,
)
from .native_operations.removals import DIRECT_OPERATIONS as REMOVAL_DIRECT_OPERATIONS
from .native_operations.zones import DIRECT_OPERATIONS as ZONE_DIRECT_OPERATIONS
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
    "helpers.registry.snapshot": ("ha_mcp_tools/registries", False),
    "helpers.entities.for_entry": ("ha_mcp_tools/registry_lookup", False),
    "integrations.entries": ("ha_mcp_tools/config_entries", False),
}
DOMAIN_DIRECT_OPERATIONS = {
    **HELPER_DIRECT_OPERATIONS,
    **ZONE_DIRECT_OPERATIONS,
    **FLOW_DIRECT_OPERATIONS,
    **LISTING_DIRECT_OPERATIONS,
    **CONFIG_FINALIZE_OPERATIONS,
    **REGISTRY_MUTATION_OPERATIONS,
    **AREA_FLOOR_OPERATIONS,
    **REMOVAL_DIRECT_OPERATIONS,
    **INTEGRATION_READ_OPERATIONS,
    **INTEGRATION_INSPECTION_OPERATIONS,
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
        redaction_enabled: Callable[[], bool] | None = None,
        code_handler: Callable[[dict[str, Any], CallContext], Awaitable[Any]]
        | None = None,
        helper_backup: Callable[
            [dict[str, Any], CallContext], Awaitable[dict[str, Any] | None]
        ]
        | None = None,
        todo_backup: Callable[
            [dict[str, Any], CallContext], Awaitable[dict[str, Any] | None]
        ]
        | None = None,
        config_backup: Callable[
            [dict[str, Any], CallContext], Awaitable[dict[str, Any] | None]
        ]
        | None = None,
        registry_backup: Callable[
            [dict[str, Any], CallContext], Awaitable[dict[str, Any] | None]
        ]
        | None = None,
    ) -> None:
        from .websocket_api import _command_specs

        self.hass = hass
        self.service_policy = service_policy
        self.skill_guide_reader = skill_guide_reader or SkillGuideReader()
        self.skill_content_enabled = skill_content_enabled or (lambda: True)
        self.redaction_enabled = redaction_enabled or (lambda: False)
        self.code_handler = code_handler
        self.helper_backup = helper_backup
        self.todo_backup = todo_backup
        self.config_backup = config_backup
        self.registry_backup = registry_backup
        self.loop = asyncio.get_running_loop()
        self._commands: dict[str, tuple[Any, Any, Any]] = {}
        self._flows: Any = None
        self._templates: Any = None
        self._reload_watches: Any = None
        self._entry_watches: Any = None
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
        if operation in {
            "config.flow.abort",
            "config.reload.close",
            "config.entries.close",
        }:
            # These can only release a nonce already owned by this actor/client.
            # Revoking credentials must not strand its pending flow or listener.
            self._check_envelope(arguments)
            return await self._cleanup_owned(operation, arguments, context)
        await self._authorize_operation(operation, context)
        self._check_envelope(arguments)
        from .native_operations.todo import TODO_OPERATIONS, NativeTodoOperations

        if operation in TODO_OPERATIONS:
            result = await NativeTodoOperations(self).execute(
                operation, arguments, context
            )
            return json.loads(json_bytes(result))
        if operation == "config.flow.settings":
            return {"redact_secrets": self.redaction_enabled()}
        if operation in REGISTRY_DIRECT_OPERATIONS:
            result = await REGISTRY_DIRECT_OPERATIONS[operation](self.hass, arguments)
            return json.loads(json_bytes(result))
        if operation in DOMAIN_DIRECT_OPERATIONS:
            function, writes = DOMAIN_DIRECT_OPERATIONS[operation]
            if writes:
                check_mutation_admission(context)
            if writes and context.read_only:
                raise ToolFailure(
                    "READ_ONLY",
                    "Writes are disabled for this caller.",
                    status="not_started",
                )
            result = await function(self.hass, arguments)
            return json.loads(json_bytes(result))
        if operation in COMMAND_OPERATIONS:
            return await self._execute_command(operation, arguments, context)
        return await self._execute_owned_operation(operation, arguments, context)

    async def _authorize_operation(self, operation: str, context: CallContext) -> None:
        try:
            await self.authorize(context)
        except PermissionError as error:
            if operation in {"config.flow.start", "config.flow.configure"}:
                raise ToolFailure(
                    "AUTH_REQUIRED", "Flow admission was revoked.", status="not_started"
                ) from error
            raise

    @staticmethod
    def _check_envelope(arguments: dict[str, Any]) -> None:
        if {"type", "id", "ha_mcp_context"}.intersection(arguments):
            raise ToolFailure(
                "INVALID_ARGUMENTS",
                "Protected operation envelope field.",
                status="not_started",
            )

    async def _cleanup_owned(
        self, operation: str, arguments: dict[str, Any], context: CallContext
    ) -> Any:
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("Native cleanup must run on the HA event loop")
        if operation == "config.flow.abort" and self._flows is not None:
            return await self._flows.abort_owned(arguments["flow_id"], context)
        if operation == "config.reload.close" and self._reload_watches is not None:
            return await self._reload_watches.execute(operation, arguments, context)
        if operation == "config.entries.close" and self._entry_watches is not None:
            return await self._entry_watches.execute(operation, arguments, context)
        raise UnsupportedOperation(operation)

    async def _execute_owned_operation(
        self, operation: str, arguments: dict[str, Any], context: CallContext
    ) -> Any:
        if operation in {"config.entries.watch", "config.entries.observe"}:
            from .native_operations.entry_watches import NativeEntryWatches

            if self._entry_watches is None:
                self._entry_watches = NativeEntryWatches(self.hass, self.authorize)
            return await self._entry_watches.execute(operation, arguments, context)
        from .native_operations.integration_state import (
            INTEGRATION_STATE_OPERATIONS,
            NativeIntegrationState,
        )
        from .native_operations.removals import REMOVAL_OPERATIONS, NativeRemovals

        if operation in INTEGRATION_STATE_OPERATIONS:
            result = await NativeIntegrationState(self.hass, self.authorize).execute(
                operation, arguments, context
            )
            return json.loads(json_bytes(result))
        if operation in REMOVAL_OPERATIONS:
            result = await NativeRemovals(self.hass, self.authorize).execute(
                operation, arguments, context
            )
            return json.loads(json_bytes(result))
        if operation == "code.run" and self.code_handler is not None:
            return await self.code_handler(arguments, context)
        if operation in {
            *(
                f"config.{domain}.{action}"
                for domain in ("automation", "script", "scene")
                for action in ("write", "delete")
            ),
            "config.flow.start",
            "config.flow.configure",
            "config.flow.abort",
            "config.reload.watch",
            "config.reload.wait",
            "config.reload.close",
        }:
            return await self._execute_config_operation(operation, arguments, context)
        return await self._runtime_operation(operation, arguments, context)

    async def _runtime_operation(
        self, operation: str, arguments: dict[str, Any], context: CallContext
    ) -> Any:
        if operation in {
            "helpers.backup.capture",
            "config.backup.capture",
            "registry.backup.capture",
        }:
            return await self._capture_backup(operation, arguments, context)
        if operation == "skills.guide":
            return await self.hass.async_add_executor_job(
                self.skill_guide_reader.read, arguments.get("file", "SKILL.md")
            )
        if operation == "skills.files":
            enabled = self.skill_content_enabled()
            return await self.hass.async_add_executor_job(
                lambda: read_skill_files(arguments["references"], enabled=enabled)
            )
        if operation == "instance.timezone":
            return {"timezone": self.hass.config.time_zone, "fetch_failed": False}
        if operation == "services.policy":
            self._check_service_policy(arguments["domain"], arguments["service"])
            return None
        raise UnsupportedOperation(operation)

    async def _execute_config_operation(
        self, operation: str, arguments: dict[str, Any], context: CallContext
    ) -> Any:
        if operation in {
            f"config.{domain}.{action}"
            for domain in ("automation", "script", "scene")
            for action in ("write", "delete")
        }:
            from .native_operations.config_mutations import NativeConfigMutations

            owner = NativeConfigMutations(self.hass, self.authorize)
            if self._reload_watches is not None:
                owner.hook_runner = self._reload_watches.run_hook
            result = await owner.execute(operation, arguments, context)
            return json.loads(json_bytes(result))
        if operation in {
            "config.flow.start",
            "config.flow.configure",
            "config.flow.abort",
        }:
            from .native_operations.flows import NativeFlows, _unknown_flow_outcome

            if self._flows is None:
                self._flows = NativeFlows(self.hass, self.authorize)
            result = await self._flows.execute(operation, arguments, context)
            try:
                return json.loads(json_bytes(result))
            except (TypeError, ValueError) as error:
                _unknown_flow_outcome(error)
        from .native_operations.reload_watches import NativeReloadWatches

        if self._reload_watches is None:
            self._reload_watches = NativeReloadWatches(self.hass, self.authorize)
        return await self._reload_watches.execute(operation, arguments, context)

    async def _execute_command(
        self, operation: str, arguments: dict[str, Any], context: CallContext
    ) -> Any:
        if operation not in COMMAND_OPERATIONS:
            raise UnsupportedOperation(operation)
        command, writes = COMMAND_OPERATIONS[operation]
        if writes:
            check_mutation_admission(context)
        if writes and context.read_only:
            raise ToolFailure("READ_ONLY", "Writes are disabled for this caller.")
        schema, do, prep = self._commands[command]
        payload = dict(arguments)
        if operation == "helpers.storage.write":
            from .native_operations.helpers import check_expected_item

            expected = payload.pop("expected_item", None)
            expected_registry = payload.pop("expected_registry", None)
            payload.pop("wait_for_entity", None)
            check_expected_item(self.hass, payload, expected)
        include_changed_states = (
            payload.pop("include_changed_states", False)
            if operation == "services.call_observe"
            else False
        )
        message = schema({"type": command, **payload})
        message["ha_mcp_context"] = Context(user_id=context.user_id)
        if operation == "helpers.storage.write":
            from .native_operations.helper_mutations import NativeHelperMutations

            result = await NativeHelperMutations(self.hass, self.authorize).execute(
                {
                    **message,
                    "expected_item": expected,
                    "expected_registry": expected_registry,
                },
                context,
            )
            return json.loads(json_bytes(result))
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
        errors = []
        for owner in (
            self._flows,
            self._reload_watches,
            self._entry_watches,
            self._templates,
        ):
            if owner is not None:
                try:
                    await owner.stop()
                except Exception as error:  # noqa: BLE001 - every owner gets cleanup, then failure is raised
                    errors.append(error)
        if errors:
            raise RuntimeError("native_resources_not_closed") from errors[0]

    async def _capture_backup(
        self, operation: str, arguments: dict[str, Any], context: CallContext
    ) -> dict[str, Any]:
        check_mutation_admission(context)
        if context.read_only:
            raise ToolFailure(
                "READ_ONLY",
                "Writes are disabled for this caller.",
                status="not_started",
            )
        capture = {
            "helpers.backup.capture": self.helper_backup,
            "config.backup.capture": self.config_backup,
            "registry.backup.capture": self.registry_backup,
            "todo.backup.capture": self.todo_backup,
        }[operation]
        if capture is not None:
            return await capture(arguments, context) or {}
        return {
            "warnings": [
                "Automatic pre-write backup is unavailable in this native entry."
            ]
        }
