"""Single public restart/reload contracts shared with the Core-native service."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from pydantic import Field

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure
from .system_values import RELOAD_TARGETS
from .validation import validate_identifier_not_empty


class SystemActionsFacade:
    def __init__(
        self,
        restart: Callable[[bool], Awaitable[dict[str, Any]]],
        reload: Callable[[str, str | None], Awaitable[dict[str, Any]]],
    ) -> None:
        self._restart_call = restart
        self._reload_call = reload

    async def ha_restart(
        self,
        confirm: Annotated[
            bool, Field(description="Must be True to confirm the restart.")
        ] = False,
        safe_mode: Annotated[
            bool,
            Field(
                description="Restart in safe mode: no custom integrations or custom frontend resources load until the next normal restart."
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Execute a Home Assistant restart.

        WARNING: restarts the entire Home Assistant instance; all automations
        are unavailable during the restart, which typically takes 1-5 minutes.
        Config is validated automatically before the restart proceeds (to
        pre-check, call ha_get_system_health(include="config_check")). For
        configuration changes, consider ha_reload_core() instead, which reloads
        specific components without a full restart. When this server runs
        inside Home Assistant (embedded), it replies before the restart starts
        and is unreachable until Home Assistant is back; with safe_mode it
        stays down until Home Assistant is next restarted normally, because it
        is itself a custom integration.

        EXAMPLE: ha_restart(confirm=True)
        """
        if not confirm:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Restart not confirmed",
                    details=(
                        "You must set confirm=True to restart Home Assistant. "
                        "This is a safety measure to prevent accidental restarts."
                    ),
                    suggestions=[
                        'Pre-check config via ha_get_system_health(include="config_check")',
                        "Call ha_restart(confirm=True) to proceed with restart",
                        "Consider using ha_reload_core() for config-only changes",
                    ],
                )
            )

        return await self._restart_call(safe_mode)

    async def ha_reload_core(
        self,
        target: Annotated[
            str,
            Field(
                description=(
                    "What to reload: 'all', 'automations', 'scripts', 'scenes', "
                    "'groups', 'input_booleans', 'input_numbers', 'input_texts', "
                    "'input_selects', 'input_datetimes', 'input_buttons', "
                    "'timers', 'templates', 'persons', 'zones', 'core' "
                    "(customize, packages) or 'themes'."
                )
            ),
        ] = "all",
        entry_id: Annotated[
            str | None,
            Field(
                description=(
                    "Reload a SINGLE config entry (one integration instance) "
                    "instead of sweeping subsystems — the fast path after editing "
                    "a custom component on disk. Pass it alone (leave target at "
                    "'all'); combining it with an explicit target is a validation "
                    "error. Find the id via ha_get_integration."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Execute a Home Assistant configuration reload without a full restart.

        Reloads specific configuration components so changes take effect without
        restarting Home Assistant — much faster than a full restart. Use it after
        editing automation/script YAML, adding YAML helpers, or changing
        customize.yaml or themes.

        EXAMPLES:
        - ha_reload_core(target="automations")
        - ha_reload_core(entry_id="abc123")
        """
        target = target.lower().strip()

        # Single config-entry reload (issue #1813 fold-in): reload just the
        # integration instance identified by ``entry_id`` rather than sweeping
        # every reloadable subsystem — the fast path after editing a custom
        # component on disk. ``target`` defaults to "all" (meaning "no subsystem
        # chosen"), so entry_id + the default is an entry-only reload; entry_id
        # paired with an explicit subsystem target is contradictory.
        if entry_id is not None:
            entry_id = validate_identifier_not_empty(
                entry_id,
                "entry_id",
                suggestions=[
                    "Find the entry_id via ha_get_integration",
                    "Omit entry_id to reload a whole subsystem via target",
                ],
            )
            if target != "all":
                raise_failure(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "entry_id cannot be combined with a specific reload "
                        f"target ('{target}')",
                        context={"entry_id": entry_id, "target": target},
                        suggestions=[
                            "Pass entry_id alone to reload one config entry",
                            f"Omit entry_id to reload the '{target}' subsystem",
                        ],
                    )
                )
            return await self._reload_call(target, entry_id)

        if target not in RELOAD_TARGETS:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid reload target: {target}",
                    context={
                        "target": target,
                        "valid_targets": list(RELOAD_TARGETS.keys()),
                    },
                    suggestions=[f"Use one of: {', '.join(RELOAD_TARGETS.keys())}"],
                )
            )

        return await self._reload_call(target, None)
