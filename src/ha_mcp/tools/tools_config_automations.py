"""
Configuration management tools for Home Assistant automations.

This module provides tools for retrieving, creating, updating, and removing
Home Assistant automation configurations.
"""

import logging
from typing import Any, NoReturn

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import legacy_errors, legacy_errors_sync
from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ..errors import (
    ErrorCode,
    create_error_response,
)
from ..runtime.automation_config import AutomationConfigMethods
from ..runtime.automation_config import (
    _action_contains_scene_create as _action_contains_scene_create,
)
from ..runtime.automation_config import (
    _detect_conflicting_root_keys as _detect_conflicting_root_keys,
)
from ..runtime.automation_config import (
    _scene_create_in_choose as _scene_create_in_choose,
)
from ..runtime.automation_config import (
    _strip_redundant_identifier_echo as _strip_redundant_identifier_echo,
)
from ..runtime.automation_config import (
    _validate_automation_identifier as _validate_identifier,
)
from ..runtime.automation_writes import AutomationWriteTools
from ..runtime.config_normalize import (
    _normalize_automation_config as _normalize_automation_config,
)
from ..runtime.config_normalize import (
    _normalize_config_for_roundtrip as _normalize_config_for_roundtrip,
)
from ..runtime.config_normalize import (
    _normalize_trigger_keys as _normalize_trigger_keys,
)
from ..runtime.config_reads import AutomationReadTools
from ..runtime.config_removals import AutomationRemovalTools
from ..utils.python_sandbox import (
    safe_execute,
)
from .auto_backup import automation_backup_target, with_auto_backup
from .best_practice_checker import (
    BestPracticeCheckResult,
)
from .component_config_reads import fetch_entity_lookup_via_component
from .config_helpers.registry import validate_registry_ids
from .config_write_helpers import (
    apply_entity_category,
    attach_skill_content,
    augment_error_dict_with_skill_content,
    augment_tool_error_with_skill_content,
    config_reload_waiter,
    fetch_entity_category,
)
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .reference_validator import validate_config_references
from .tool_hints import read_only_hints, write_hints
from .ws_waiters import (
    verify_entity_removed,
    wait_for_automation_entity_by_unique_id,
    wait_for_entity_registered,
    wait_for_state_change,
)

logger = logging.getLogger(__name__)
_validate_automation_identifier = legacy_errors_sync(_validate_identifier)


# Skill files attached to ha_config_set_automation responses when
# MandatoryBPS=True (default), plus auto-attached on best-practice
# warning hits regardless of MandatoryBPS. Paths are relative to the
# home-assistant-best-practices skill directory.
_AUTOMATION_SKILL_FILES: tuple[str, ...] = (
    "references/triggers-and-conditions.md",
    "references/automation-actions.md",
    "references/template-guidelines.md",
)


# Distinctive prefix of the soft-failure warning emitted by
# ``ha_config_set_automation`` when ``_poll_for_automation_entity``
# exhausts its budget (``_POLL_BUDGET_S``) without resolving the new
# automation's entity_id. Exported so future tests can detect a missed
# registration without hard-coding the literal — rewording the warning
# becomes a compile-time coupling rather than a silent test drift.
NOT_VERIFIED_WARNING_PREFIX = (
    "Automation was submitted to Home Assistant but the entity was not found"
)


# Keys whose value is a free-form user namespace rather than part of HA's
# action schema. Names inside them are the user's own variable names, so no
# alias mapping may rewrite them (a variable named 'sequences' is data, not a
# misspelled 'sequence' key).


def _skip_automation_runtime_backup(kwargs: dict[str, Any]) -> bool:
    """Skip config snapshots for runtime-only calls (enabled / run_actions)."""
    return (
        (kwargs.get("enabled") is not None or bool(kwargs.get("run_actions")))
        and kwargs.get("config") is None
        and kwargs.get("python_transform") is None
        and not kwargs.get("take_control_of_blueprint")
    )


async def _resolve_post_write_automation_entity(
    client: Any,
    identifier: str | None,
    entity_id: str | None,
    wait: bool,
    response: dict[str, Any],
) -> str | None:
    """Resolve a raw unique ID after a write when registration may lag."""
    if (
        wait
        and not entity_id
        and identifier
        and not identifier.startswith("automation.")
    ):
        try:
            return await wait_for_automation_entity_by_unique_id(client, identifier)
        except (HomeAssistantConnectionError, HomeAssistantAuthError) as e:
            response.setdefault("warnings", []).append(
                f"Automation registration verification failed: {e}"
            )
    return entity_id


async def _resolve_runtime_target(
    client: Any, identifier: str, response: dict[str, Any]
) -> str | None:
    """Resolve the entity to receive a runtime change (enabled / run_actions)."""
    if identifier.startswith("automation."):
        return identifier
    # A config write reloads the automation and replaces its entity when the
    # config changed (or creates it), so poll even when the caller passed
    # wait=False.
    return await _resolve_post_write_automation_entity(
        client, identifier, None, True, response
    )


# Standalone runtime calls raise on a service failure; after a config write the
# same failure is a partial-success warning because the write already landed.
_STANDALONE_RUNTIME_ACTIONS = ("set_enabled", "run_actions")


async def _set_automation_enabled(client: Any, entity_id: str, enabled: bool) -> Any:
    """Set an automation's runtime enabled state through Home Assistant."""
    service = "turn_on" if enabled else "turn_off"
    return await client.call_service("automation", service, {"entity_id": entity_id})


class AutomationConfigTools(AutomationWriteTools):
    """Configuration management tools for Home Assistant automations."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _transform_config(self, expression: str, config: dict[str, Any]) -> Any:
        return safe_execute(expression, config)

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
        attach_skill_content(
            response,
            MandatoryBPS=MandatoryBPS,
            canonical_files=canonical_files,
            referenced_files=referenced_files,
        )

    async def _augment_error_dict(
        self, error: dict[str, Any], bp_warnings: Any
    ) -> None:
        augment_error_dict_with_skill_content(error, bp_warnings)

    async def _validate_references(self, config: dict[str, Any]) -> dict[str, Any]:
        return await validate_config_references(self._client, config)

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

    async def _wait_registered(self, entity_id: str) -> bool:
        return await wait_for_entity_registered(self._client, entity_id)

    async def _resolve_post_write_automation_entity(
        self,
        identifier: str | None,
        entity_id: str | None,
        wait: bool,
        response: dict[str, Any],
    ) -> str | None:
        return await _resolve_post_write_automation_entity(
            self._client, identifier, entity_id, wait, response
        )

    async def _resolve_runtime_target(
        self, identifier: str, response: dict[str, Any]
    ) -> str | None:
        return await _resolve_runtime_target(self._client, identifier, response)

    async def _automation_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> None:
        await self._client.call_service(domain, service, data)

    async def _runtime_enable(self, entity_id: str, enabled: bool) -> None:
        await _set_automation_enabled(self._client, entity_id, enabled)

    async def _wait_enabled(self, entity_id: str, expected_state: str) -> Any:
        return await wait_for_state_change(
            self._client, entity_id, expected_state=expected_state
        )

    def _runtime_error(self, error: Exception, context: dict[str, Any]) -> NoReturn:
        exception_to_structured_error(error, context=context)

    def _config_reload_waiter(self, *, enabled: bool) -> Any:
        return config_reload_waiter(
            self._client, "automation_reloaded", enabled=enabled
        )

    async def _raise_automation_write_error(
        self,
        error: Exception,
        identifier: str | None,
        bp_warnings: BestPracticeCheckResult,
    ) -> NoReturn:
        if isinstance(error, ToolError):
            raise augment_tool_error_with_skill_content(error, bp_warnings) from None
        await AutomationWriteTools._raise_automation_write_error(
            self, error, identifier, bp_warnings
        )

    async def _resolve_automation_entity_id(
        self, identifier: str, *, allow_component: bool = False
    ) -> str | None:
        """Resolve an automation identifier to its entity_id.

        If identifier is already an entity_id (starts with "automation."),
        returns it directly. Otherwise, searches states to find the entity
        whose unique_id matches the identifier.

        When ``allow_component`` (remove/post-write call sites only — a
        config-get must never route through the component, see
        ``component_config_reads`` and ``TestConfigGetSeam``) AND the component
        advertises ``entity_lookup``, one in-process
        ``entity_lookup(unique_id=identifier, domain="automation")`` frame
        replaces the whole-``get_states()`` scan. An automation's registry
        ``unique_id`` IS its config ``id`` — the same value the legacy scan
        matches against ``attributes["id"]`` — so the two paths resolve the same
        entity_id (pinned by the cross-seam contract test). A component miss
        (empty matches) returns ``None``, exactly like the legacy no-match. The
        component unavailable / errored ⇒ the legacy ``get_states()`` scan below
        runs unchanged (issue #1813 Phase 2).
        """
        if identifier.startswith("automation."):
            return identifier
        if allow_component:
            matches = await fetch_entity_lookup_via_component(
                self._client, identifier, domain="automation"
            )
            if matches is not None:
                # In-process registry read: authoritative on return.
                for match in matches:
                    entity_id = match.get("entity_id") or ""
                    if entity_id.startswith("automation."):
                        return entity_id
                return None
        try:
            states = await self._client.get_states()
            for state in states:
                if (
                    state.get("entity_id", "").startswith("automation.")
                    and state.get("attributes", {}).get("id") == identifier
                ):
                    return str(state["entity_id"])
        except Exception as e:  # noqa: BLE001
            logger.debug(
                f"Failed to resolve entity_id for automation {identifier}: {e}"
            )
        return None

    async def _resolve_automation_entity_id_strict(self, identifier: str) -> str | None:
        """Resolve an identifier without hiding transport/authentication errors."""
        states = await self._client.get_states()
        for state in states:
            state_entity_id = state.get("entity_id", "")
            if state_entity_id.startswith("automation.") and (
                state_entity_id == identifier
                or state.get("attributes", {}).get("id") == identifier
            ):
                return str(state_entity_id)
        return None

    ha_config_get_automation = tool(
        name="ha_config_get_automation",
        tags={"Automations"},
        annotations=read_only_hints("Get Automation Config", open_world=False),
    )(log_tool_usage(legacy_errors(AutomationReadTools.ha_config_get_automation)))

    def _map_read_error(self, error: Exception, identifier: str) -> NoReturn:
        if isinstance(error, ToolError):
            raise error
        exception_to_structured_error(
            error,
            context={"identifier": identifier, "action": "get"},
            suggestions=[
                "Verify automation exists using ha_search(domain_filter='automation')",
                "Check Home Assistant connection",
                "Use ha_get_skill_guide for help",
            ],
        )

    async def _fetch_category(
        self, entity_id: str, scope: str, warnings: list[str]
    ) -> str | None:
        return await fetch_entity_category(self._client, entity_id, scope, warnings)

    _legacy_get_automation = AutomationReadTools._read_automation

    _read_automation = AutomationReadTools._read_automation

    ha_config_set_automation = tool(
        name="ha_config_set_automation",
        tags={"Automations"},
        annotations=write_hints(
            "Create or Update Automation",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )(
        with_auto_backup(
            domain="automation",
            id_fn=automation_backup_target,
            skip_fn=_skip_automation_runtime_backup,
        )(log_tool_usage(legacy_errors(AutomationWriteTools.ha_config_set_automation)))
    )

    _build_set_automation_suggestions = staticmethod(
        legacy_errors_sync(AutomationConfigMethods._build_set_automation_suggestions)
    )

    async def _upsert_automation(
        self,
        config: dict[str, Any],
        identifier: str | None,
        resolved_id: str | None,
    ) -> dict[str, Any]:
        """Upsert, threading a pre-resolved unique_id when available.

        When ``resolved_id`` is set the caller already resolved ``identifier``
        (via the hash check or alias guard); pass it with ``_resolved=True`` so the
        REST client skips the redundant entity_id→unique_id lookup (issue #1813
        Phase 0). Otherwise fall back to the raw ``identifier`` and let the REST
        client resolve — creation and fetched configs without an id use this
        fallback.
        """
        result: dict[str, Any]
        if resolved_id is not None:
            result = await self._client.upsert_automation_config(
                config, resolved_id, _resolved=True
            )
        else:
            result = await self._client.upsert_automation_config(config, identifier)
        return result

    _take_control_config = legacy_errors(
        AutomationConfigMethods._take_control_config, classify=False
    )

    _maybe_set_runtime_only = legacy_errors(
        AutomationWriteTools._maybe_set_runtime_only, classify=False
    )

    _set_runtime_only = legacy_errors(
        AutomationWriteTools._set_runtime_only, classify=False
    )

    _apply_runtime_state = legacy_errors(
        AutomationWriteTools._apply_runtime_state, classify=False
    )

    _apply_enabled_state = legacy_errors(
        AutomationWriteTools._apply_enabled_state, classify=False
    )

    _run_automation_actions = legacy_errors(
        AutomationWriteTools._run_automation_actions, classify=False
    )

    _verify_enabled_state = legacy_errors(
        AutomationWriteTools._verify_enabled_state, classify=False
    )

    _run_python_transform = legacy_errors(
        AutomationWriteTools._run_python_transform, classify=False
    )

    _run_config_update = legacy_errors(
        AutomationWriteTools._run_config_update, classify=False
    )

    async def _list_automation_entity_ids(self) -> list[str]:
        """Best-effort list of automation entity_ids (up to 10) from the entity registry.

        Used to populate ``available_automation_ids`` in RESOURCE_NOT_FOUND
        error context. Returns an empty list on any failure — caller treats
        absence as "no IDs to report" rather than failing the structured
        error raise. The 10-entry cap lives here (not at the callers) so a
        new call site can't accidentally bloat the error payload.
        """
        try:
            result = await self._client.send_websocket_message(
                {"type": "config/entity_registry/list"}
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("Failed to list automation entity_ids from registry: %s", e)
            return []
        entries = result.get("result", []) if isinstance(result, dict) else result
        if not isinstance(entries, list):
            return []
        return [
            entry["entity_id"]
            for entry in entries
            if isinstance(entry, dict)
            and isinstance(entry.get("entity_id"), str)
            and entry["entity_id"].startswith("automation.")
        ][:10]

    _guard_alias_replacement = legacy_errors(
        AutomationConfigMethods._guard_alias_replacement, classify=False
    )

    async def _raise_automation_not_found(self, identifier: str) -> NoReturn:
        """Raise a structured RESOURCE_NOT_FOUND ToolError for a missing automation.

        Single source of truth for the 404→RESOURCE_NOT_FOUND mapping used
        by both the GET path (``_get_automation_config_internal``) and the
        mutation paths (``ha_config_set_automation`` update branch,
        ``ha_config_remove_automation``). Populates
        ``available_automation_ids`` (up to 10) from the entity registry.
        """
        available_ids = await self._list_automation_entity_ids()
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Automation not found: {identifier}",
                context={
                    "automation_id": identifier,
                    "available_automation_ids": available_ids,
                },
                suggestions=[
                    "Use ha_search(domain_filter='automation') to find existing automations"
                ],
            )
        )

    _get_automation_config_internal = legacy_errors(
        AutomationConfigMethods._get_automation_config_internal, classify=False
    )

    _fetch_and_verify_hash = legacy_errors(
        AutomationConfigMethods._fetch_and_verify_hash, classify=False
    )

    _parse_and_validate_config = staticmethod(
        legacy_errors_sync(AutomationConfigMethods._parse_and_validate_config)
    )

    _check_scene_create_misroute = staticmethod(
        legacy_errors_sync(AutomationConfigMethods._check_scene_create_misroute)
    )

    _validate_condition_platform = staticmethod(
        legacy_errors_sync(AutomationConfigMethods._validate_condition_platform)
    )

    _validate_required_fields = staticmethod(
        legacy_errors_sync(AutomationConfigMethods._validate_required_fields)
    )

    async def _read_automation_config(self, identifier: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._client.get_automation_config(identifier)
        return result

    async def _render_blueprint(
        self, domain: str, path: str, inputs: dict[str, Any]
    ) -> dict[str, Any]:
        from .blueprint_substitute import substitute_blueprint

        result: dict[str, Any] = await substitute_blueprint(
            self._client, domain, path, inputs
        )
        return result

    ha_config_remove_automation = tool(
        name="ha_config_remove_automation",
        tags={"Automations"},
        annotations=write_hints(
            "Remove Automation", destructive=True, idempotent=True, open_world=False
        ),
    )(
        with_auto_backup(domain="automation", id_param="identifier")(
            log_tool_usage(
                legacy_errors(AutomationRemovalTools.ha_config_remove_automation)
            )
        )
    )

    async def _delete_automation(self, identifier: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._client.delete_automation_config(identifier)
        return result

    async def _verify_removed(self, entity_id: str, result: dict[str, Any]) -> None:
        await verify_entity_removed(self._client, entity_id, result)

    async def _map_remove_error(self, e: Exception, identifier: str) -> NoReturn:
        if isinstance(e, ToolError):
            raise e
        if isinstance(e, HomeAssistantAPIError) and e.status_code == 404:
            await self._raise_automation_not_found(identifier)
        exception_to_structured_error(
            e,
            context={"identifier": identifier, "action": "delete"},
            suggestions=[
                "Verify automation exists using ha_search(domain_filter='automation')",
                "Use entity_id format: automation.morning_routine or unique_id",
                "Check Home Assistant connection",
            ],
        )


def register_config_automation_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant automation configuration tools."""
    register_tool_methods(mcp, AutomationConfigTools(client))
