"""Defer self-affecting requests, then hand them to Core without a drain cycle."""

import asyncio
import logging
from contextvars import Context as TaskContext
from dataclasses import replace
from functools import partial
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
        action: Literal["restart", "entry_reload", "update_install", "backup_restore"],
        actor: CallContext,
        *,
        entry: Any = None,
        update: Any = None,
        backup: bool = False,
        update_service: Any = None,
        restore_target: Any = None,
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
            self._deliver(
                action,
                actor,
                entry,
                self._policy_key(),
                update,
                backup,
                update_service,
                restore_target,
            ),
            name=f"ha-mcp-deferred-{action}",
        )
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def _deliver(
        self,
        action: str,
        actor: CallContext,
        entry: Any,
        policy_key: str | None,
        update: Any = None,
        backup: bool = False,
        update_service: Any = None,
        restore_target: Any = None,
    ) -> None:
        try:
            # This is a response-flush allowance, not a promise of delivery or
            # restart completion. The public result reports only scheduling.
            await asyncio.sleep(self.reply_delay)
            await self.manager.refresh_policy()
            await admit_registry_write(actor, self.manager.backend.authorize)
            await self._verify_deferred_target(action, actor, update, restore_target)
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
            if action == "update_install":
                from .update_entities import check_update_target, stopping_update

                check_update_target(hass, update)
                if (
                    not stopping_update(hass, update)
                    or update_service is None
                    or hass.services.async_services_for_domain("update").get("install")
                    is not update_service
                ):
                    raise ToolFailure(
                        "CONFIG_CONFLICT", "The self-affecting update owner changed"
                    )
            # Core will unload this manager. From this point the request belongs
            # to Core, so its unload must never wait on the task that requested it.
            task = asyncio.current_task()
            if task is not None:
                self.pending.discard(task)
            self._handoff(action, actor, entry, update, backup, restore_target)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - the replied caller cannot receive a deferred failure
            _LOGGER.warning(
                "Deferred %s was refused or failed (%s). Issue a new call after checking the current policy and Core state.",
                action,
                getattr(error, "code", type(error).__name__),
            )

    async def _verify_deferred_target(
        self, action: str, actor: CallContext, update: Any, restore_target: Any
    ) -> None:
        if action == "update_install":
            from .updates import NativeUpdates

            await NativeUpdates(self.manager, actor)._visible_target(update.entity_id)
            await admit_registry_write(actor, self.manager.backend.authorize)
        elif action == "backup_restore":
            from .full_backup_restore import FullRestoreTarget

            if not isinstance(restore_target, FullRestoreTarget):
                raise ValueError("A full restore requires a pinned target")
            await restore_target.verify(self.manager, actor)

    def _handoff(
        self,
        action: str,
        actor: CallContext,
        entry: Any,
        update: Any,
        backup: bool,
        restore_target: Any = None,
    ) -> None:
        hass = self.manager.hass
        clean = TaskContext()
        if action == "entry_reload":
            clean.run(hass.config_entries.async_schedule_reload, entry.entry_id)
        elif action == "backup_restore":
            request = restore_target.owner.async_restore_backup(
                restore_target.backup_id, **restore_target.arguments()
            )
            owner_task = clean.run(
                hass.async_create_task, request, "ha-mcp requested backup restore"
            )
            owner_task.add_done_callback(partial(_completed_handoff, action=action))
        else:
            from homeassistant.core import Context

            request = hass.services.async_call(
                "update" if action == "update_install" else "homeassistant",
                "install" if action == "update_install" else "restart",
                {"entity_id": update.entity_id, "backup": backup}
                if action == "update_install"
                else {},
                blocking=False,
                context=Context(user_id=actor.user_id),
            )
            owner_task = clean.run(
                hass.async_create_task, request, f"ha-mcp requested {action}"
            )
            owner_task.add_done_callback(partial(_completed_handoff, action=action))

    async def stop(self) -> None:
        self.closed = True
        current = asyncio.current_task()
        tasks = tuple(task for task in self.pending if task is not current)
        for task in tasks:
            task.cancel()
        if tasks:
            await await_owned_io(asyncio.gather(*tasks, return_exceptions=True))


def _completed_handoff(task: asyncio.Task[Any], *, action: str) -> None:
    if task.cancelled():
        return
    if error := task.exception():
        _LOGGER.warning(
            "Core did not accept the deferred %s request (%s).",
            action,
            type(error).__name__,
        )
