"""Defer self-affecting requests, then hand them to Core without a drain cycle."""

import asyncio
import logging
from contextvars import Context as TaskContext
from dataclasses import replace
from typing import Any, Literal

from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.owned_executor import await_owned_io

from .registry_access import admit_registry_write

_LOGGER = logging.getLogger(__name__)


class NativeSystemLifecycle:
    def __init__(self, manager: Any, *, reply_delay: float = 1.0) -> None:
        self.manager = manager
        self.reply_delay = reply_delay
        self.pending: set[asyncio.Task[None]] = set()
        self.closed = False

    def _policy_key(self) -> str | None:
        from ..native_approvals import _policy_hash

        policy = self.manager.policy.security_policy
        return _policy_hash(policy) if policy is not None else None

    def schedule(
        self,
        action: Literal["restart", "entry_reload"],
        actor: CallContext,
        *,
        entry: Any = None,
    ) -> None:
        check_mutation_admission(actor)
        if self.closed:
            raise ToolFailure(
                "INTEGRATION_STOPPING",
                "The integration is stopping.",
                status="not_started",
            )
        if any(not task.done() for task in self.pending):
            raise ToolFailure(
                "LIFECYCLE_PENDING",
                "A restart or self-reload request is already pending.",
                status="not_started",
            )
        if action == "entry_reload" and entry is not self.manager.entry:
            raise ValueError("Only this manager's entry requires deferred reload")
        # There is no request left to receive progress after the reply. The
        # credential validator and scoped actor remain live until dispatch.
        actor = replace(actor, progress=None)
        task = asyncio.create_task(
            self._deliver(action, actor, entry, self._policy_key()),
            name=f"ha-mcp-deferred-{action}",
        )
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def _deliver(
        self, action: str, actor: CallContext, entry: Any, policy_key: str | None
    ) -> None:
        try:
            # This is a response-flush allowance, not a promise of delivery or
            # restart completion. The public result reports only scheduling.
            await asyncio.sleep(self.reply_delay)
            await self.manager.refresh_policy()
            await admit_registry_write(actor, self.manager.backend.authorize)
            if self.closed or not self.manager.accepting:
                raise ToolFailure(
                    "INTEGRATION_STOPPING", "The integration is stopping."
                )
            if self._policy_key() != policy_key:
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "The approval policy changed after the request was admitted.",
                )
            hass = self.manager.hass
            if (
                action == "entry_reload"
                and hass.config_entries.async_get_entry(entry.entry_id) is not entry
            ):
                raise ToolFailure(
                    "CONFIG_CONFLICT", "The config entry owner changed before reload."
                )
            check_mutation_admission(actor)
            # Core will unload this manager. From this point the request belongs
            # to Core, so its unload must never wait on the task that requested it.
            task = asyncio.current_task()
            if task is not None:
                self.pending.discard(task)
            clean = TaskContext()
            if action == "entry_reload":
                clean.run(hass.config_entries.async_schedule_reload, entry.entry_id)
            else:
                from homeassistant.core import Context

                request = hass.services.async_call(
                    "homeassistant",
                    "restart",
                    {},
                    blocking=False,
                    context=Context(user_id=actor.user_id),
                )
                owner_task = clean.run(
                    hass.async_create_task, request, "ha-mcp requested Core restart"
                )
                owner_task.add_done_callback(_completed_handoff)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - the replied caller cannot receive a deferred failure
            _LOGGER.warning(
                "Deferred %s was refused or failed (%s). Issue a new call after checking the current policy and Core state.",
                action,
                getattr(error, "code", type(error).__name__),
            )

    async def stop(self) -> None:
        self.closed = True
        current = asyncio.current_task()
        tasks = tuple(task for task in self.pending if task is not current)
        for task in tasks:
            task.cancel()
        if tasks:
            await await_owned_io(asyncio.gather(*tasks, return_exceptions=True))


def _completed_handoff(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    if error := task.exception():
        _LOGGER.warning(
            "Core did not accept the deferred restart request (%s).",
            type(error).__name__,
        )
