"""Frozen standalone restart/reload acquisition; native execution uses Core owners."""

import asyncio
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.runtime.service_outcome import project_service_reload_results
from ha_mcp.runtime.system_values import (
    RELOAD_TARGETS,
    reload_entry_result,
    reload_target_result,
)
from ha_mcp.tools.helpers import exception_to_structured_error, raise_tool_error
from ha_mcp.tools.system_restart import restart_home_assistant


class LegacySystemActions:
    _client: Any

    async def _restart_call(self, safe_mode: bool = False) -> dict[str, Any]:
        return await restart_home_assistant(
            self._client, confirm=True, safe_mode=safe_mode
        )

    async def _reload_call(self, target: str, entry_id: str | None) -> dict[str, Any]:
        if entry_id is not None:
            return await self._reload_config_entry(entry_id)
        try:
            if target == "all":
                return await self._reload_all_targets()

            else:
                # Reload specific component
                service_info = RELOAD_TARGETS[target]
                if service_info is None:
                    # This shouldn't happen as we check for "all" above
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.INTERNAL_ERROR,
                            f"Invalid target configuration for: {target}",
                            context={"target": target},
                        )
                    )
                domain, service = service_info
                await self._client.call_service(domain, service, {})

                return reload_target_result(target, domain, service)

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(
                e,
                context={"target": target},
                suggestions=[
                    f"Ensure the {target} integration is loaded",
                    "Check Home Assistant logs for details",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises

    async def _reload_all_targets(self) -> dict[str, Any]:
        """Reload every reloadable subsystem concurrently, capped by a semaphore.

        Extracted verbatim from ``ha_reload_core``'s ``target == "all"`` branch.
        """
        # Reload all reloadable components. Fire the calls concurrently
        # but cap the in-flight count with a semaphore so a large
        # install doesn't hit HA with ~16 simultaneous service calls.
        # A single failing target must not cancel its siblings, so the
        # calls are gathered with ``return_exceptions=True`` and
        # attributed per target below (preserving RELOAD_TARGETS order).
        reloadable = [
            (name, info) for name, info in RELOAD_TARGETS.items() if info is not None
        ]
        semaphore = asyncio.Semaphore(4)

        async def _reload_one(domain: str, service: str) -> None:
            async with semaphore:
                await self._client.call_service(domain, service, {})

        outcomes = await asyncio.gather(
            *(_reload_one(domain, service) for _, (domain, service) in reloadable),
            return_exceptions=True,
        )

        return project_service_reload_results(reloadable, outcomes)

    async def _reload_config_entry(self, entry_id: str) -> dict[str, Any]:
        """Reload a single config entry via the REST config-entries endpoint.

        POSTs to ``/config/config_entries/entry/{entry_id}/reload`` through the
        REST client's generic request method — the ``/entry/`` path segment the
        hand-rolled workaround in issue #1813 was easy to get wrong. A 404
        surfaces as a not-found error naming the ``entry_id``; other failures
        route through the shared exception classifier. Returns the tool's
        standard envelope plus ``reloaded``/``entry_id``.
        """
        try:
            resp = await self._client._request(
                "POST", f"/config/config_entries/entry/{entry_id}/reload"
            )
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            # Local import mirrors ``_reraise_if_fatal``: rest_client imports
            # from the tool helpers transitively, so a module-level import
            # would risk a circular import in the tools package.
            from ..client.rest_client import HomeAssistantAPIError

            if isinstance(e, HomeAssistantAPIError) and e.status_code == 404:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        f"Config entry not found: {entry_id}",
                        context={"entry_id": entry_id},
                        suggestions=[
                            "Verify the entry_id via ha_get_integration",
                        ],
                    )
                )
            if isinstance(e, HomeAssistantAPIError) and e.status_code == 403:
                # Core returns 403 "Entry cannot be reloaded" for
                # OperationNotAllowed — an entry whose integration does not
                # support reload — not an auth problem.
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Config entry {entry_id} cannot be reloaded "
                        "(the integration does not support reload)",
                        context={"entry_id": entry_id},
                        suggestions=[
                            "Restart Home Assistant (ha_restart) to apply "
                            "changes to this integration",
                        ],
                    )
                )
            exception_to_structured_error(
                e,
                context={"entry_id": entry_id},
                suggestions=[
                    "Verify the entry_id via ha_get_integration",
                    "Check Home Assistant logs for details",
                ],
            )

        # Core reports require_restart=True when the entry could not be
        # hot-reloaded (its state is not recoverable) — a success response
        # that still needs a restart to take effect.
        require_restart = isinstance(resp, dict) and bool(resp.get("require_restart"))
        return reload_entry_result(entry_id, require_restart)
