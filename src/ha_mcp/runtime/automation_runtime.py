"""Automation runtime actions and post-write confirmation without transport assumptions."""

import logging
from typing import Any, NoReturn

from ha_mcp.errors import (
    ErrorCode,
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
    create_error_response,
)
from ha_mcp.tool_api import ToolFailure

from .automation_config import AutomationConfigMethods
from .errors import raise_classified, raise_failure

logger = logging.getLogger(__name__)
_RUNTIME_ERRORS = (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
    ToolFailure,
    PermissionError,
    TimeoutError,
    OSError,
)

_STANDALONE_RUNTIME_ACTIONS = ("set_enabled", "run_actions")


def _sync_post_write_automation_result(
    response: dict[str, Any], entity_id: str | None
) -> None:
    """Record a successfully resolved entity and clear stale poll state."""
    if entity_id:
        response["entity_id"] = entity_id
        response.pop("entity_not_verified", None)


def _run_actions_once_reloaded(
    result: dict[str, Any],
    reloaded: bool | None,
    run_actions: bool,
    identifier: str | None,
) -> bool:
    """Keep run_actions after a write only once the reload is confirmed.

    Unlike ``enabled``, a run cannot be corrected afterwards: before the reload
    it would run the previous actions, or hit no entity at all.
    """
    if not run_actions or reloaded:
        return run_actions
    result["actions_triggered"] = False
    result.setdefault("warnings", []).append(
        "Automation was written, but its actions were not run: the reload the "
        "write scheduled could not be confirmed, so they could have run the "
        "previous version. Run them with ha_config_set_automation("
        f"identifier='{identifier or '<automation id>'}', run_actions=True)."
    )
    return False


class AutomationRuntimeTools(AutomationConfigMethods):
    async def _resolve_automation_entity_id_strict(self, identifier: str) -> str | None:
        result: str | None = await self.backend.execute(
            "config.automation.runtime_target", {"identifier": identifier}, self.context
        )
        return result

    async def _resolve_post_write_automation_entity(
        self,
        identifier: str | None,
        entity_id: str | None,
        wait: bool,
        response: dict[str, Any],
    ) -> str | None:
        if (
            wait
            and not entity_id
            and identifier
            and not identifier.startswith("automation.")
        ):
            try:
                return await self._wait_automation_entity(identifier)
            except _RUNTIME_ERRORS as error:
                response.setdefault("warnings", []).append(
                    f"Automation registration verification failed: {error}"
                )
        return entity_id

    async def _wait_automation_entity(self, identifier: str) -> str | None:
        result: str | None = await self.backend.execute(
            "config.entity_id.after_write",
            {"storage_key": identifier, "domain": "automation", "timeout": 6},
            self.context,
        )
        return result

    async def _resolve_runtime_target(
        self, identifier: str, response: dict[str, Any]
    ) -> str | None:
        if identifier.startswith("automation."):
            return identifier
        return await self._resolve_post_write_automation_entity(
            identifier, None, True, response
        )

    async def _automation_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> None:
        await self._runtime_service(domain, service, data)

    async def _runtime_enable(self, entity_id: str, enabled: bool) -> None:
        await self._automation_service(
            "automation", "turn_on" if enabled else "turn_off", {"entity_id": entity_id}
        )

    async def _wait_enabled(self, entity_id: str, expected_state: str) -> Any:
        return await self.backend.execute(
            "entities.state.wait",
            {"entity_id": entity_id, "expected_state": expected_state},
            self.context,
        )

    def _runtime_error(self, error: Exception, context: dict[str, Any]) -> NoReturn:
        raise_classified(error, context)

    async def _maybe_set_runtime_only(
        self,
        identifier: str | None,
        config: Any,
        python_transform: str | None,
        category: str | None,
        enabled: bool | None,
        run_actions: bool,
        wait: bool,
    ) -> dict[str, Any] | None:
        """Handle a standalone runtime request (enabled / run_actions), if any."""
        if (enabled is None and not run_actions) or (
            config is not None or python_transform is not None
        ):
            return None
        if category is not None:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "category requires a config update",
                    suggestions=[
                        "Pass config or python_transform when assigning a category",
                        "Omit category for a standalone enabled or run_actions call",
                    ],
                    context={"action": "set_runtime", "category": category},
                )
            )
        return await self._set_runtime_only(
            identifier, enabled, wait, run_actions=run_actions
        )

    async def _set_runtime_only(
        self,
        identifier: str | None,
        enabled: bool | None,
        wait: bool,
        *,
        run_actions: bool = False,
    ) -> dict[str, Any]:
        """Apply enabled and/or run_actions to an existing automation, no config."""
        action = "set_enabled" if enabled is not None else "run_actions"
        if not identifier:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "identifier is required when setting enabled or run_actions "
                    "without config",
                    suggestions=[
                        "Pass an automation entity_id or unique_id",
                        "Use ha_search(domain_filter='automation') to find automations",
                    ],
                    context={"action": action, "enabled": enabled},
                )
            )
        entity_id = await self._resolve_automation_entity_id_strict(identifier)
        if not entity_id:
            await self._raise_automation_not_found(identifier)
        response: dict[str, Any] = {
            "success": True,
            "action": action,
            "automation_id": entity_id,
        }
        await self._apply_runtime_state(
            response,
            entity_id,
            enabled,
            wait,
            identifier=identifier,
            run_actions=run_actions,
        )
        return response

    async def _apply_runtime_state(
        self,
        response: dict[str, Any],
        entity_id: str | None,
        enabled: bool | None,
        wait: bool,
        *,
        identifier: str | None = None,
        run_actions: bool = False,
    ) -> str | None:
        """Apply enabled and then run_actions without touching the stored config.

        Resolves ``entity_id`` from ``identifier`` when it is None. On a
        standalone call a service failure raises a ToolError; after a config
        write it is reported as a partial-success warning, since the write
        itself already landed. Returns the resolved entity_id.
        """
        if enabled is None and not run_actions:
            return entity_id
        if entity_id is None and identifier:
            entity_id = await self._resolve_runtime_target(identifier, response)
        if entity_id is None:
            retry_args: list[str] = []
            if enabled is not None:
                response["enabled_requested"] = enabled
                response["enabled_applied"] = False
                retry_args.append(f"enabled={enabled}")
            if run_actions:
                response["actions_triggered"] = False
                retry_args.append("run_actions=True")
            retry_id = identifier or response.get("unique_id") or "<automation id>"
            response.setdefault("warnings", []).append(
                "Automation was written, but its entity_id could not be resolved; "
                "the requested runtime change was not applied. Retry with "
                f"ha_config_set_automation(identifier='{retry_id}', "
                f"{', '.join(retry_args)}) once the automation is loaded."
            )
            return None
        if enabled is not None:
            await self._apply_enabled_state(
                response, entity_id, enabled, wait, identifier=identifier
            )
        if run_actions:
            await self._run_automation_actions(
                response, entity_id, identifier=identifier
            )
        return entity_id

    async def _apply_enabled_state(
        self,
        response: dict[str, Any],
        entity_id: str,
        enabled: bool,
        wait: bool,
        *,
        identifier: str | None = None,
    ) -> None:
        """Turn the resolved automation on or off, then optionally verify it."""
        try:
            await self._runtime_enable(entity_id, enabled)
        except _RUNTIME_ERRORS as exc:
            if response.get("action") in _STANDALONE_RUNTIME_ACTIONS:
                self._runtime_error(
                    exc,
                    context={
                        "action": "set_enabled",
                        "identifier": identifier,
                        "entity_id": entity_id,
                        "enabled": enabled,
                    },
                )
            logger.warning(
                "Enabled state not applied to %s after config write: %s",
                entity_id,
                exc,
            )
            response["enabled_requested"] = enabled
            response["enabled_applied"] = False
            response.setdefault("warnings", []).append(
                "Automation config was written, but the requested enabled state "
                f"could not be applied: {exc}"
            )
            return
        response["enabled"] = enabled
        response["enabled_applied"] = True
        if wait:
            await self._verify_enabled_state(response, entity_id, enabled)

    async def _run_automation_actions(
        self,
        response: dict[str, Any],
        entity_id: str,
        *,
        identifier: str | None = None,
    ) -> None:
        """Run the automation's actions now via automation.trigger."""
        try:
            await self._automation_service(
                "automation", "trigger", {"entity_id": entity_id}
            )
        except _RUNTIME_ERRORS as exc:
            # With enabled in the same standalone call, the state change has
            # already landed, so a failed run is a partial success, not an error.
            if response.get("action") == "run_actions":
                self._runtime_error(
                    exc,
                    context={
                        "action": "run_actions",
                        "identifier": identifier,
                        "entity_id": entity_id,
                    },
                )
            logger.warning(
                "Actions not run for %s after config write: %s", entity_id, exc
            )
            response["actions_triggered"] = False
            response.setdefault("warnings", []).append(
                f"The requested change was applied, but the actions could not be run: {exc}"
            )
            return
        response["actions_triggered"] = True

    async def _verify_enabled_state(
        self, response: dict[str, Any], entity_id: str, enabled: bool
    ) -> None:
        """Wait for the automation to report the requested state."""
        expected_state = "on" if enabled else "off"
        try:
            verified = await self._wait_enabled(entity_id, expected_state)
        except _RUNTIME_ERRORS as exc:
            response.setdefault("warnings", []).append(
                f"Automation {entity_id} was sent {expected_state}, but state "
                f"verification failed: {exc}"
            )
            return
        if verified is None:
            response.setdefault("warnings", []).append(
                f"Automation {entity_id} was sent {expected_state} but its state "
                "could not be verified before the timeout."
            )
