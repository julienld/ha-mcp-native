"""Remote automation identity and Core metadata reads."""

from __future__ import annotations

import logging
from typing import Any, NamedTuple, NoReturn, cast

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantError,
)
from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tools.component_automation import automation_lookup
from ha_mcp.tools.config_write_helpers import fetch_registry_entry
from ha_mcp.tools.helpers import raise_tool_error
from ha_mcp.tools.util_helpers import list_visible_registry_ids

logger = logging.getLogger(__name__)

_REFERENCES_NEED_COMPONENT = (
    "references need the ha_mcp_tools component's automation_config bridge, "
    "which is not available"
)

_REFERENCES_NOT_LOADED = (
    "references unavailable: Home Assistant has not loaded this automation "
    "(disabled, or it failed to load)"
)


class _Target(NamedTuple):
    """An automation resolved for a read or a write."""

    config_id: str
    entity_id: str | None
    # The component's lookup reply; None when the component did not answer or
    # was not asked (``with_entity=False``).
    lookup: dict[str, Any] | None


class RemoteAutomationReads:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def _resolve_target(
        self, entity_id: str, *, with_entity: bool = True
    ) -> _Target:
        """Resolve ``entity_id`` (an entity_id or a config id) to its config id.

        An entity_id resolves through the registry (the component's lookup,
        else the REST client's registry-then-state resolver), so a disabled
        automation resolves too; one nothing knows is RESOURCE_NOT_FOUND, never
        a creation. A config id is used as given; ``with_entity`` also looks up
        its entity.
        """
        if entity_id.startswith("automation."):
            lookup = await automation_lookup(self._client, entity_id=entity_id)
            if lookup is None:
                try:
                    config_id = await self._client._resolve_automation_id(entity_id)
                except HomeAssistantAPIError as e:
                    if e.status_code == 404:
                        await self._raise_automation_not_found(entity_id)
                    raise
                return _Target(config_id, entity_id, None)
            unique_id = (lookup.get("entity") or {}).get("unique_id")
            if not lookup["found"] or not unique_id:
                await self._raise_automation_not_found(entity_id)
            return _Target(str(unique_id), entity_id, lookup)
        if not with_entity:
            return _Target(entity_id, None, None)
        lookup = await automation_lookup(self._client, unique_id=entity_id)
        if lookup is None:
            return _Target(
                entity_id, await self._entity_id_from_states(entity_id), None
            )
        row = lookup.get("entity") if lookup["found"] else None
        return _Target(entity_id, (row or {}).get("entity_id"), lookup)

    async def _read_stored(self, config_id: str) -> dict[str, Any] | None:
        """The body Core stores under ``config_id``, verbatim; None when absent."""
        try:
            return cast(
                dict[str, Any], await self._client.get_automation_config(config_id)
            )
        except HomeAssistantAPIError as e:
            if e.status_code == 404:
                return None
            raise

    async def _registry_details(
        self, target: _Target, warnings: list[str]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
        """The automation's registry row, Core's validation verdict and references."""
        lookup = target.lookup
        if lookup is not None:
            references = lookup.get("references")
            if references is None and lookup.get("loaded") is False:
                warnings.append(_REFERENCES_NOT_LOADED)
            return lookup.get("entity"), lookup.get("validation"), references
        warnings.append(_REFERENCES_NEED_COMPONENT)
        if target.entity_id is None:
            return None, None, None
        entity = await fetch_registry_entry(self._client, target.entity_id, warnings)
        return entity, await self._repair_validation(target.entity_id, warnings), None

    async def _repair_validation(
        self, entity_id: str, warnings: list[str]
    ) -> dict[str, Any] | None:
        """Core's validation verdict from the repair issue it raises for a failed load."""
        try:
            result = await self._client.send_websocket_message(
                {"type": "repairs/list_issues"}
            )
        except HomeAssistantError as e:
            warnings.append(f"validation status unavailable: {e}")
            return None
        if not result.get("success"):
            reason = result.get("error") or "request failed"
            warnings.append(f"validation status unavailable: {reason}")
            return None
        prefix = f"{entity_id}_validation_"
        for issue in (result.get("result") or {}).get("issues") or []:
            issue_id = issue.get("issue_id")
            if (
                issue.get("domain") != "automation"
                or not isinstance(issue_id, str)
                or not issue_id.startswith(prefix)
            ):
                continue
            status = issue_id.removeprefix(prefix)
            # Another remainder belongs to an automation whose entity_id
            # extends this one (automation.x_validation_failed_y).
            if status.startswith("failed_") and "_validation_" not in status:
                placeholders = issue.get("translation_placeholders") or {}
                return {"status": status, "error": placeholders.get("error")}
        return None

    async def _raise_automation_not_found(
        self, entity_id: str, target: _Target | None = None
    ) -> NoReturn:
        """Raise RESOURCE_NOT_FOUND with ``available_automation_ids`` (up to 10).

        A ``target`` the registry resolved has an automation Core's config API
        cannot reach: one defined in YAML or a package, CONFIG_NOT_FOUND.
        """
        if target is not None and target.entity_id:
            row = (target.lookup or {}).get("entity") or {}
            raise_tool_error(
                create_error_response(
                    ErrorCode.CONFIG_NOT_FOUND,
                    f"Automation {target.entity_id} exists (config id "
                    f"{target.config_id}) but is not stored in automations.yaml, "
                    "so Home Assistant's config API cannot read, edit or remove "
                    "it; it is defined in YAML or a package.",
                    context={
                        "automation_id": target.entity_id,
                        "config_id": target.config_id,
                        "registry_platform": row.get("platform"),
                    },
                    suggestions=["Edit it in the YAML file or package defining it"],
                )
            )
        available_ids = await list_visible_registry_ids(self._client, "automation")
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Automation not found: {entity_id}",
                context={
                    "automation_id": entity_id,
                    "available_automation_ids": available_ids,
                },
                suggestions=[
                    "Use ha_search(domain_filter='automation') to find existing automations"
                ],
            )
        )

    async def _entity_id_from_states(self, config_id: str) -> str | None:
        try:
            states = await self._client.get_states()
        except (HomeAssistantError, TimeoutError, OSError) as error:
            logger.debug("Automation state discovery unavailable: %s", error)
            return None
        if not isinstance(states, list):
            return None
        for state in states:
            if (
                isinstance(state, dict)
                and str(state.get("entity_id", "")).startswith("automation.")
                and state.get("attributes", {}).get("id") == config_id
            ):
                return str(state["entity_id"])
        return None

    async def read(self, identifier: str) -> dict[str, Any]:
        target = await self._resolve_target(identifier)
        config = await self._read_stored(target.config_id)
        if config is None:
            await self._raise_automation_not_found(identifier, target)
        warnings: list[str] = []
        entity, validation, references = await self._registry_details(target, warnings)
        return {
            "found": True,
            "config": config,
            "storage_key": target.config_id,
            "entity_id": target.entity_id,
            "entity": entity,
            "validation": validation,
            "references": references,
            "warnings": warnings,
        }
