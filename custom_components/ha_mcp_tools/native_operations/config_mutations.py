"""Persist config with Core's real validator, view lock, writer and reload hook."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Callable, Coroutine
from importlib import import_module
from typing import Any, NoReturn

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.json import json_bytes

from ha_mcp.errors import create_validation_error
from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation
from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.owned_executor import await_owned_io

from ..helper_collections import _INVALID_ERRORS
from .core_views import config_view
from .owner import CoreOperationOwner

CONFIG_MUTATION_OPERATIONS = {
    f"config.{domain}.{action}": (domain, action)
    for domain in ("automation", "script", "scene")
    for action in ("write", "delete")
}


class NativeConfigMutations(CoreOperationOwner):
    """A bounded semantic operation, never a request/view/WS impersonation."""

    hook_runner: (
        Callable[
            [str, Coroutine[Any, Any, None], CallContext], Coroutine[Any, Any, None]
        ]
        | None
    ) = None

    async def execute(
        self, operation: str, arguments: dict[str, Any], context: CallContext
    ) -> dict[str, Any]:
        if operation not in CONFIG_MUTATION_OPERATIONS:
            raise UnsupportedOperation(operation)
        domain, action = CONFIG_MUTATION_OPERATIONS[operation]
        owner = config_view(self.hass, domain)
        core = import_module("homeassistant.components.config.view")
        constants = import_module("homeassistant.components.config.const")
        writer = core._write
        key = arguments["storage_key"]
        value = copy.deepcopy(arguments.get("data"))
        await self.authorize(context)
        self._admit(context)
        if action == "write":
            if not isinstance(value, dict):
                raise ToolFailure(
                    "INVALID_ARGUMENTS",
                    "Config must be an object.",
                    status="not_started",
                )
            await self._validate(owner, key, value)
        interrupted: asyncio.CancelledError | None = None
        async with owner.mutation_lock:
            current = await owner.read_config(self.hass)
            previous = owner._get_value(self.hass, current, key)
            if not json_values_equal(
                json.loads(json_bytes(previous)), arguments["expected_config"]
            ):
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "The config changed after its snapshot was read; no mutation was sent.",
                    status="not_started",
                )
            if action == "delete" and previous is None:
                raise ToolFailure(
                    "RESOURCE_NOT_FOUND",
                    f"{domain} not found: {key}",
                    status="not_started",
                )
            # Validation and the fresh persisted read can yield. Recheck the
            # actor/tool policy after those awaits, before touching Core data.
            await self.authorize(context)
            self._admit(context)
            if action == "delete":
                owner._delete_value(self.hass, current, key)
            else:
                owner._write_value(self.hass, current, key, value)
            interrupted = await _persist_config(
                self.hass, writer, self.hass.config.path(owner.path), current
            )
        hook = owner.post_write_hook
        if hook is not None:
            hook_operation = hook(
                constants.ACTION_DELETE
                if action == "delete"
                else constants.ACTION_CREATE_UPDATE,
                key,
            )
            if self.hook_runner is not None:
                hook_operation = self.hook_runner(domain, hook_operation, context)
            self.hass.async_create_task(hook_operation)
        if interrupted is not None:
            raise interrupted
        # Core acknowledges persisted content and schedules its hook. A caller
        # that needs reload/registration confirmation must observe that event.
        return {
            "result": "ok",
            "operation": "deleted" if action == "delete" else "updated",
            f"{domain}_id": key,
        }

    @staticmethod
    def _admit(context: CallContext) -> None:
        check_mutation_admission(context)
        if context.read_only:
            raise ToolFailure(
                "READ_ONLY",
                "Writes are disabled for this caller.",
                status="not_started",
            )

    async def _validate(self, owner: Any, key: str, value: dict[str, Any]) -> None:
        try:
            owner.key_schema(key)
        except _INVALID_ERRORS as error:
            _invalid_config(error, "Key")
        try:
            if owner.data_validator is not None:
                await owner.data_validator(self.hass, key, value)
            elif owner.data_schema is not None:
                owner.data_schema(value)
            else:
                raise UnsupportedOperation("config.write.validator")
        except (*_INVALID_ERRORS, HomeAssistantError) as error:
            _invalid_config(error, "Message")


def _invalid_config(error: BaseException, kind: str) -> NoReturn:
    data = create_validation_error(f"{kind} malformed: {error}")
    raise ToolFailure(
        data["error"]["code"], data["error"]["message"], data=data, status="not_started"
    ) from error


async def _persist_config(
    hass: HomeAssistant, writer: Callable[..., Any], path: str, current: Any
) -> asyncio.CancelledError | None:
    worker = asyncio.ensure_future(hass.async_add_executor_job(writer, path, current))
    try:
        await await_owned_io(worker)
    except asyncio.CancelledError as error:
        if worker.cancelled() or worker.exception() is not None:
            raise
        # The atomic write succeeded while the caller was cancelled. The owner
        # must still schedule Core's hook exactly once, then propagate it.
        return error
    return None
