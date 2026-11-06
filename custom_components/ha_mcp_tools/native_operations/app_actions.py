"""Finite app mutations on Core's Supervisor client, with no transport fallback."""

import asyncio
import copy
from collections.abc import Awaitable, Callable
from importlib import import_module
from typing import Any

from ha_mcp.runtime.app_store import (
    config_submitted_result,
    store_refresh_result,
    store_versions,
)
from ha_mcp.runtime.app_values import _merge_options
from ha_mcp.runtime.redaction import (
    collect_addon_secret_values,
    register_known_secret_values,
)
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation
from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.owned_executor import await_owned_io
from ha_mcp.utils.registry_update_lock import registry_update_lock

from ..native_server_policy import setting_value
from .app_reads import NativeAppReads
from .app_transport import AppHTTPError
from .registry_access import admit_registry_write

_ACTIONS = {
    "install": ("store", "install_addon", 1800),
    "update": ("store", "update_addon", 1800),
    "rebuild": ("addons", "rebuild_addon", 1800),
    "start": ("addons", "start_addon", 120),
    "stop": ("addons", "stop_addon", 60),
    "restart": ("addons", "restart_addon", 120),
    "uninstall": ("addons", "uninstall_addon", 120),
}
STORE_RELOAD_TIMEOUT = 300
WEBSOCKET_TIMEOUT = 145
_CONFIG_FIELDS = (
    "slug",
    "version",
    "options",
    "network",
    "boot",
    "auto_update",
    "watchdog",
)


def app_operation_timeout(arguments: dict[str, Any]) -> float | None:
    """Allow the provider's job window plus preparation and readback."""
    action = str(arguments.get("action") or "").strip().lower()
    if action in _ACTIONS:
        return _ACTIONS[action][2] + 120
    if action == "check_updates":
        return STORE_RELOAD_TIMEOUT + 120
    if arguments.get("websocket"):
        return WEBSOCKET_TIMEOUT + 120
    return None


class AppActions(NativeAppReads):
    def __init__(
        self, manager: Any, actor: CallContext, arguments: dict[str, Any]
    ) -> None:
        super().__init__(manager, actor)
        self.arguments = arguments
        self.completed = False
        self.array_patch_post_completed: bool | None = None

    async def admit(self) -> None:
        await self.manager.visibility.before(
            "ha_manage_app", self.arguments, self.actor
        )
        await admit_registry_write(self.actor, self.manager.backend.authorize)
        self.supervisor.pin()

    async def observe(self) -> None:
        try:
            await self.supervisor.authorize()
        except ToolFailure as error:
            if not self.completed:
                raise
            raise self.confirmed_failure(error) from None

    def confirmed_failure(self, error: ToolFailure) -> ToolFailure:
        failure = ToolFailure(error.code, str(error), status="confirmed")
        failure.data.update(write_completed=True, requires_reread=True)
        if self.array_patch_post_completed is not None:
            failure.data.update(
                prerequisite_get_completed=True,
                array_patch_applied=self.array_patch_post_completed,
                write_completed=self.array_patch_post_completed,
            )
        return failure

    async def write[T](
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        budget: float = 120,
        array_patch_post: bool = False,
    ) -> T:
        async def worker() -> T:
            await self.admit()
            try:
                async with asyncio.timeout(budget):
                    result = await operation()
            except AppHTTPError:
                # Preserve a received status for route-specific guidance. It
                # does not prove an opaque request had no effects or succeeded.
                try:
                    await self.supervisor.authorize()
                except ToolFailure as denied:
                    denied.status = "outcome_unknown"
                    raise
                raise
            except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 - provider may fail after effects
                # Even an API error may follow a completed job or a partial
                # provider change. Never reinterpret it as safe to replay.
                raise ToolFailure(
                    "OUTCOME_UNKNOWN",
                    f"The app operation was sent to Supervisor ({type(error).__name__}). "
                    "Inspect the app before another change; no retry was performed.",
                    status="outcome_unknown",
                ) from None
            self.completed = True
            if array_patch_post:
                self.array_patch_post_completed = True
            await self.observe()
            return result

        return await await_owned_io(worker())

    async def _execute_action_mode(self, slug: str, action: str) -> dict[str, Any]:
        key = action.strip().lower()
        if key not in _ACTIONS:
            raise ToolFailure(
                "VALIDATION_ERROR",
                f"Invalid app action: {action!r}",
                status="not_started",
            )
        async with registry_update_lock("supervisor_app", slug):
            await self.admit()
            family, method_name, budget = _ACTIONS[key]
            method = getattr(getattr(self.supervisor.client, family), method_name)
            await self.write(lambda: method(slug), budget=budget)
            return {
                "success": True,
                "action": key,
                "slug": slug,
                "message": f"App (add-on) {slug} {key} completed.",
            }

    async def _versions(
        self, warnings: list[str], when: str
    ) -> dict[str, dict[str, Any]] | None:
        try:
            return store_versions(await self.supervisor.store())
        except ToolFailure as error:
            if error.code in {"AUTH_FORBIDDEN", "CONFIG_CONFLICT"}:
                await self.observe()
                raise
            warnings.append(
                f"Could not read the app store {when} the reload: {error.code}"
            )
            return None

    async def _execute_check_updates(self) -> dict[str, Any]:
        async with registry_update_lock("supervisor_store", "repositories"):
            await self.admit()
            warnings: list[str] = []
            before = await self._versions(warnings, "before")
            await self.write(
                self.supervisor.client.store.reload, budget=STORE_RELOAD_TIMEOUT
            )
            after = await self._versions(warnings, "after")
            await self.observe()
            return store_refresh_result(before, after, warnings)

    async def _execute_repository_action(
        self, action: str, repository: str
    ) -> dict[str, Any]:
        key = action.strip().lower()
        async with registry_update_lock("supervisor_store", "repositories"):
            await self.admit()
            StoreAddRepository = import_module(
                "aiohasupervisor.models.store"
            ).StoreAddRepository
            data = await self.supervisor.store()
            rows = data.get("repositories")
            if not isinstance(rows, list) or any(
                not isinstance(row, dict) or not row.get("slug") for row in rows
            ):
                raise UnsupportedOperation("Supervisor repository inventory")
            present = any(
                row.get("source" if key == "add_repository" else "slug") == repository
                for row in rows
            )
            if present == (key == "add_repository"):
                await self.admit()
                verb = "already registered" if present else "not registered"
                return {
                    "success": True,
                    "action": key,
                    "repository": repository,
                    "message": f"Repository {repository} is {verb}; no change needed.",
                }
            if key == "add_repository":
                proposal = StoreAddRepository(repository=repository)
                await self.write(
                    lambda: self.supervisor.client.store.add_repository(proposal)
                )
            else:
                await self.write(
                    lambda: self.supervisor.client.store.remove_repository(repository)
                )
            return {
                "success": True,
                "action": key,
                "repository": repository,
                "message": f"Repository {repository} {key} completed.",
            }

    async def _execute_config_mode(
        self, slug: str, config_data: dict[str, Any]
    ) -> dict[str, Any]:
        async with registry_update_lock("supervisor_app", slug):
            await self.admit()
            AddonsOptions = import_module("aiohasupervisor.models.addons").AddonsOptions
            before = await self.supervisor.app_info(slug)
            proposal = copy.deepcopy(config_data)
            if "options" in proposal:
                # Shared mode validation rejects redaction placeholders before
                # a merge can replace a credential with its display sentinel.
                from ha_mcp.runtime.app_dispatch import AppDispatch

                AppDispatch._reject_sentinel_options(proposal["options"])
                proposal["options"] = _merge_options(
                    before.get("options", {}), proposal["options"]
                )
                if self.manager.policy.flag("redact_secrets") and isinstance(
                    before.get("schema"), list
                ):
                    option_keys = str(
                        setting_value(
                            self.manager.policy.flags, "redact_app_option_keys"
                        )
                        or ""
                    )
                    register_known_secret_values(
                        collect_addon_secret_values(
                            before.get("options", {}),
                            before["schema"],
                            slug,
                            option_keys=option_keys,
                        )
                        | collect_addon_secret_values(
                            proposal["options"],
                            before["schema"],
                            slug,
                            option_keys=option_keys,
                        )
                    )
                validation = await self._validate_options(slug, proposal["options"])
                await self.admit()
                if not validation.valid:
                    raise ToolFailure(
                        "VALIDATION_ERROR",
                        "Supervisor rejected the proposed app options. No configuration was written.",
                        status="not_started",
                    )
            model = AddonsOptions.from_dict(copy.deepcopy(proposal))
            # No native UI-schema copy: Supervisor's validator and request model
            # own accepted values. Its API has no conditional options update.
            current = await self.supervisor.app_info(slug)
            if not json_values_equal(_configuration(before), _configuration(current)):
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "App configuration changed during validation",
                    status="not_started",
                )
            await self.write(
                lambda: self.supervisor.client.addons.set_addon_options(slug, model)
            )
            actual = await self.supervisor.app_info(slug)
            await self.observe()
            result = config_submitted_result(slug, proposal, [])
            result["post_write_verified"] = all(
                key in actual and json_values_equal(value, actual[key])
                for key, value in model.to_dict().items()
            )
            if not result["post_write_verified"]:
                result.setdefault("warnings", []).append(
                    "Supervisor accepted the configuration but its current values differ from the submitted values. "
                    "Read ha_get_app before another change; no retry was performed."
                )
            return result

    async def _validate_options(self, slug: str, options: dict[str, Any]) -> Any:
        async def validate() -> Any:
            await self.admit()
            try:
                async with asyncio.timeout(30):
                    return await self.supervisor.client.addons.addon_config_validate(
                        slug, copy.deepcopy(options)
                    )
            except Exception as error:  # noqa: BLE001 - provider validation errors may echo credentials
                raise ToolFailure(
                    "SUPERVISOR_VALIDATION_FAILED",
                    f"Supervisor option validation failed ({type(error).__name__})",
                    status="not_started",
                ) from None

        return await await_owned_io(validate())


def _configuration(info: dict[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(info[key]) for key in _CONFIG_FIELDS if key in info}
