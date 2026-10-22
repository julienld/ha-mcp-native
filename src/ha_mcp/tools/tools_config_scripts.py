"""
Configuration management tools for Home Assistant scripts.

This module provides tools for retrieving, creating, updating, and removing
Home Assistant script configurations.
"""

import logging
from typing import Any, NoReturn, cast

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import legacy_errors, legacy_errors_sync
from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from ..runtime.config_reads import ScriptReadTools
from ..runtime.config_removals import ScriptRemovalTools
from ..runtime.script_config import ScriptConfigTools
from ..runtime.script_config import (
    _strip_empty_script_fields as _strip_empty_script_fields,
)
from ..runtime.script_writes import ScriptWriteTools
from ..utils.python_sandbox import safe_execute
from .auto_backup import with_auto_backup
from .best_practice_checker import (
    BestPracticeCheckResult,
)
from .blueprint_substitute import (
    TakenControl,
    take_control_config,
)
from .config_helpers.registry import validate_registry_ids
from .config_write_helpers import (
    apply_entity_category,
    attach_skill_content,
    augment_error_dict_with_skill_content,
    augment_tool_error_with_skill_content,
    fetch_entity_category,
)
from .entity_registration import resolve_entity_id_after_write
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .reference_validator import validate_config_references
from .tool_hints import read_only_hints, write_hints
from .ws_waiters import verify_entity_removed, wait_for_entity_registered

logger = logging.getLogger(__name__)


# Scripts attach the same files as automations: a script sequence uses
# actions, conditions and ``wait_for_trigger``. The actions file comes first
# because the strict-mode block error names the first entry.
_SCRIPT_SKILL_FILES: tuple[str, ...] = (
    "references/automation-actions.md",
    "references/triggers-and-conditions.md",
    "references/template-guidelines.md",
)


def _skip_script_run_backup(kwargs: dict[str, Any]) -> bool:
    """Skip config snapshots for a run start/stop (no config change)."""
    return kwargs.get("run") is not None


class ConfigScriptTools(ScriptWriteTools):
    async def _transform_config(self, expression: str, config: dict[str, Any]) -> Any:
        return safe_execute(expression, config)

    """Script configuration management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    ha_config_get_script = tool(
        name="ha_config_get_script",
        tags={"Scripts"},
        annotations=read_only_hints("Get Script Config", open_world=False),
    )(log_tool_usage(legacy_errors(ScriptReadTools.ha_config_get_script)))

    def _map_read_error(self, error: Exception, identifier: str) -> NoReturn:
        if isinstance(error, ToolError):
            raise error
        exception_to_structured_error(
            error,
            context={"script_id": identifier},
            suggestions=[
                "Verify script_id exists using ha_search(domain_filter='script')",
                "Check Home Assistant connection",
                "Use ha_get_skill_guide for help",
            ],
        )

    async def _fetch_category(
        self, entity_id: str, scope: str, warnings: list[str]
    ) -> str | None:
        return await fetch_entity_category(self._client, entity_id, scope, warnings)

    _legacy_get_script = ScriptReadTools._read_script

    _read_script = ScriptReadTools._read_script

    async def _list_script_entity_ids(self) -> list[str]:
        """Best-effort list of bare script IDs (up to 10) from the entity registry.

        Returns the bare storage keys (e.g. ``morning_routine``), stripping
        the ``script.`` entity_id prefix — ``ha_config_get_script`` /
        ``ha_config_set_script`` / ``ha_config_remove_script`` all take the
        bare form, so the entity_id prefix would force callers to strip it
        before retry. Returns an empty list on any failure — caller treats
        absence as "no IDs to report" rather than failing the structured
        error raise. The 10-entry cap lives here (not at the callers) so a
        new call site can't accidentally bloat the error payload.
        """
        try:
            result = await self._client.send_websocket_message(
                {"type": "config/entity_registry/list"}
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("Failed to list script entity_ids from registry: %s", e)
            return []
        entries = result.get("result", []) if isinstance(result, dict) else result
        if not isinstance(entries, list):
            return []
        return [
            entry["entity_id"][len("script.") :]
            for entry in entries
            if isinstance(entry, dict)
            and isinstance(entry.get("entity_id"), str)
            and entry["entity_id"].startswith("script.")
        ][:10]

    async def _raise_script_not_found(self, script_id: str) -> None:
        """Raise a structured RESOURCE_NOT_FOUND ToolError for a missing script.

        Single source of truth for the 404→RESOURCE_NOT_FOUND mapping used
        by the GET path (``_fetch_script_config_envelope``) and the
        mutation paths (``ha_config_set_script`` update branch,
        ``ha_config_remove_script``). Populates ``available_script_ids``
        (up to 10 bare IDs) from the entity registry.
        """
        available_ids = await self._list_script_entity_ids()
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Script not found: {script_id}",
                context={
                    "script_id": script_id,
                    "available_script_ids": available_ids,
                },
                suggestions=[
                    "Use ha_search(domain_filter='script') to find existing scripts"
                ],
            )
        )

    async def _fetch_script_config_envelope(self, script_id: str) -> dict[str, Any]:
        """Fetch the raw REST envelope, mapping 404 to RESOURCE_NOT_FOUND.

        Returns the dict envelope from ``rest_client.get_script_config``
        (``success``/``script_id``/``config`` keys). Raises a structured
        ``RESOURCE_NOT_FOUND`` ToolError via ``_raise_script_not_found`` on
        404. Other ``HomeAssistantAPIError`` instances propagate unchanged
        to caller exception handlers.
        """
        try:
            return cast(dict[str, Any], await self._client.get_script_config(script_id))
        except HomeAssistantAPIError as e:
            if e.status_code == 404:
                await self._raise_script_not_found(script_id)
            raise

    _get_script_config_internal = legacy_errors(
        ScriptConfigTools._get_script_config_internal
    )

    _fetch_and_verify_hash = legacy_errors(ScriptConfigTools._fetch_and_verify_hash)

    async def _upsert_script(
        self, config: dict[str, Any], script_id: str, resolved_id: str | None
    ) -> dict[str, Any]:
        """Upsert, threading a pre-resolved storage key when available.

        ``script_id`` is always the CALLER's identifier (used to default a
        missing alias). ``resolved_id`` — set only when ``_fetch_and_verify_hash``
        already resolved the storage key — is passed as the write target so the
        REST client skips the redundant entity-registry lookup (issue #1813
        Phase 0); None lets the REST client resolve. Passing the caller id
        separately keeps a renamed script's alias caller-facing (#1935).
        """
        result: dict[str, Any] = await self._client.upsert_script_config(
            config, script_id, resolved_id=resolved_id
        )
        return result

    _validate_script_config = staticmethod(
        legacy_errors_sync(ScriptConfigTools._validate_script_config)
    )

    ha_config_set_script = tool(
        name="ha_config_set_script",
        tags={"Scripts"},
        annotations=write_hints(
            "Create or Update Script",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )(
        with_auto_backup(
            domain="script", id_param="script_id", skip_fn=_skip_script_run_backup
        )(log_tool_usage(legacy_errors(ScriptWriteTools.ha_config_set_script)))
    )

    _prepare_script_transform = legacy_errors(
        ScriptConfigTools._prepare_script_transform
    )

    async def _take_control_config(self, script_id: str) -> TakenControl:
        current, fetched_hash, _ = await self._get_script_config_internal(script_id)
        taken, path = await take_control_config(
            self._client, "script", script_id, current
        )
        return TakenControl(taken, path, fetched_hash)

    async def _validate_categories(self, categories: dict[str, str | None]) -> None:
        await validate_registry_ids(
            self._client, None, None, categories, fail_closed=True
        )

    async def attach_skill_content(
        self,
        response: dict[str, Any],
        MandatoryBPS: bool,
        canonical_files: tuple[str, ...],
        referenced_files: set[str] | None,
    ) -> None:
        attach_skill_content(response, MandatoryBPS, canonical_files, referenced_files)

    async def _augment_error_dict(
        self, error: dict[str, Any], bp_warnings: Any
    ) -> None:
        augment_error_dict_with_skill_content(error, bp_warnings)

    async def _validate_references(self, config: dict[str, Any]) -> dict[str, Any]:
        return await validate_config_references(self._client, config)

    async def _resolve_written_entity(
        self, storage_key: str, domain: str, **options: Any
    ) -> str:
        return await resolve_entity_id_after_write(
            self._client, storage_key, domain, **options
        )

    async def _wait_registered(self, entity_id: str) -> bool:
        try:
            return await wait_for_entity_registered(self._client, entity_id)
        except (HomeAssistantConnectionError, HomeAssistantAuthError) as error:
            from ..tool_api import ToolFailure

            raise ToolFailure("VERIFICATION_FAILED", str(error)) from error

    async def _apply_category(
        self,
        entity_id: str,
        category: str,
        scope: str,
        result: dict[str, Any],
        entity_type: str,
    ) -> None:
        await apply_entity_category(
            self._client, entity_id, category, scope, result, entity_type
        )

    async def _ensure_script_exists(self, entity_id: str, script_id: str) -> None:
        try:
            await self._client.get_entity_state(entity_id)
        except HomeAssistantAPIError as error:
            if error.status_code == 404:
                await self._raise_script_not_found(script_id)
            raise

    async def _run_script_service(
        self,
        script_id: str,
        run: str | None,
        entity_id: str,
        service: str,
        data: dict[str, Any],
    ) -> None:
        try:
            await self._client.call_service("script", service, data)
        except (
            HomeAssistantAPIError,
            HomeAssistantAuthError,
            HomeAssistantConnectionError,
        ) as error:
            exception_to_structured_error(
                error,
                context={"action": run, "script_id": script_id, "entity_id": entity_id},
            )

    async def _raise_script_write_error(
        self, e: Exception, script_id: str, bp_warnings: BestPracticeCheckResult
    ) -> NoReturn:
        if isinstance(e, ToolError):
            raise augment_tool_error_with_skill_content(e, bp_warnings) from None
        suggestions = [
            "Ensure config includes either 'sequence' field (regular scripts) or 'use_blueprint' field (blueprint-based scripts)",
            "For blueprint scripts, use ha_manage_blueprints(action='list', domain='script') to list available blueprints",
            "Validate sequence actions syntax for regular scripts",
            "Check entity_ids exist if using service calls",
            "Use ha_search(domain_filter='script') to find scripts",
            "Use ha_get_skill_guide for help",
        ]
        if bp_warnings:
            suggestions.append(
                "Config had best-practice issues that may be related: "
                + "; ".join(bp_warnings)
            )
        # 404 during update only — the create path raises on its own when
        # the upsert hits an unknown identifier server-side. The bare
        # script_id form is what callers pass and what the registry stores.
        if isinstance(e, HomeAssistantAPIError) and e.status_code == 404:
            await self._raise_script_not_found(script_id)
        error = exception_to_structured_error(
            e,
            context={"script_id": script_id},
            suggestions=suggestions,
            raise_error=False,
        )
        augment_error_dict_with_skill_content(error, bp_warnings)
        raise_tool_error(error)

    ha_config_remove_script = tool(
        name="ha_config_remove_script",
        tags={"Scripts"},
        annotations=write_hints(
            "Remove Script", destructive=True, idempotent=True, open_world=False
        ),
    )(
        with_auto_backup(domain="script", id_param="script_id")(
            log_tool_usage(legacy_errors(ScriptRemovalTools.ha_config_remove_script))
        )
    )

    async def _delete_script(self, script_id: str) -> dict[str, Any]:
        return await self._client.delete_script_config(script_id)

    async def _verify_removed(self, entity_id: str, result: dict[str, Any]) -> None:
        await verify_entity_removed(self._client, entity_id, result)

    async def _map_remove_error(self, e: Exception, script_id: str) -> NoReturn:
        if isinstance(e, ToolError):
            raise e
        if isinstance(e, HomeAssistantAPIError) and e.status_code == 404:
            await self._raise_script_not_found(script_id)
        exception_to_structured_error(
            e,
            context={"script_id": script_id},
            suggestions=[
                "Verify script_id exists using ha_search(domain_filter='script')",
                "Check if script is being used by automations",
                "Use ha_get_skill_guide for help",
            ],
        )


def register_config_script_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant script configuration tools."""
    register_tool_methods(mcp, ConfigScriptTools(client))
