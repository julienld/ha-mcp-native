"""Shared entity lookup, bulk results, stable-ID resolution and enrichment."""

from __future__ import annotations

import logging
from typing import Annotated, Any, NoReturn

from pydantic import Field

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tool_api import BackendTools, ToolFailure
from ha_mcp.tools.coercion import JSON_STRING_COERCION

from .entity_records import (
    _format_fetched_entity,
    _match_registry_by_unique_id,
    _merge_entity_enrichment,
    _parse_get_entity_ids,
)
from .errors import raise_failure

logger = logging.getLogger(__name__)


class EntityDiscoveryTools(BackendTools):
    def _raise_entity_error(self, error: Exception, entity_id: Any) -> NoReturn:
        raise_failure(
            create_error_response(
                ErrorCode.INTERNAL_ERROR, str(error), context={"entity_id": entity_id}
            )
        )

    async def _fetch_entity(self, eid: str) -> dict[str, Any]:
        result = await self.backend.execute(
            "entities.registry.read",
            {"entity_ids": [eid], "single": True},
            self.context,
        )
        if eid not in result["entries"]:
            raise ValueError(result["errors"].get(eid, "Entity not found"))
        return _format_fetched_entity(result["entries"][eid])

    async def _get_entries_raw(
        self, entity_ids: list[str]
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        result = await self.backend.execute(
            "entities.registry.read", {"entity_ids": entity_ids}, self.context
        )
        return result["entries"], result["errors"]

    async def _list_entity_registry(self, unique_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = await self.backend.execute(
            "entities.registry.list", {"unique_id": unique_id}, self.context
        )
        return rows

    async def _fetch_enrichment(
        self, entity_ids: list[str]
    ) -> dict[str, dict[str, Any]] | None:
        if not entity_ids:
            return None
        result = await self.backend.execute(
            "entities.enrich", {"entity_ids": entity_ids}, self.context
        )
        if result is None:
            return None
        entities: dict[str, dict[str, Any]] = result["entities"]
        return entities

    async def _resolve_by_unique_id(
        self, unique_id: str, domain: str | None, platform: str | None
    ) -> dict[str, Any]:
        """Resolve a stable unique_id to its entity_id(s) via one registry list.

        The registry's unique key is (domain, platform, unique_id), so the same
        unique_id can appear under multiple platforms — every match is returned
        with a ``matches`` count so callers can disambiguate. ``domain`` /
        ``platform`` narrow the match set when provided.
        """
        rows = await self._list_entity_registry(unique_id)
        matches = _match_registry_by_unique_id(rows, unique_id, domain, platform)
        if not matches:
            filters = {"unique_id": unique_id}
            if domain is not None:
                filters["domain"] = domain
            if platform is not None:
                filters["platform"] = platform
            raise_failure(
                create_error_response(
                    ErrorCode.ENTITY_NOT_FOUND,
                    f"No entity found with unique_id '{unique_id}'"
                    + (
                        f" (domain={domain}, platform={platform})"
                        if domain is not None or platform is not None
                        else ""
                    ),
                    context=filters,
                    suggestions=[
                        "unique_id must match exactly — it is the integration's "
                        + "internal id, not the entity_id",
                        "Drop the domain/platform filters if you set them",
                        "Use ha_search() to browse entities and their platforms",
                    ],
                )
            )
        response: dict[str, Any] = {
            "success": True,
            "unique_id": unique_id,
            "matches": len(matches),
            "entity_entries": matches,
        }
        if domain is not None:
            response["domain"] = domain
        if platform is not None:
            response["platform"] = platform
        return response

    async def ha_get_entity(
        self,
        entity_id: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description="Entity ID or list of entity IDs to retrieve (e.g., 'sensor.temperature' or ['light.living_room', 'switch.porch']).",
                default=None,
            ),
        ] = None,
        unique_id: Annotated[
            str | None,
            Field(
                description="Stable integration unique_id (entity_id is mutable, unique_id is not). Mutually exclusive with entity_id.",
                default=None,
            ),
        ] = None,
        domain: Annotated[
            str | None,
            Field(
                description="Resolver filter (unique_id mode only): restrict matches to this entity domain, e.g. 'sensor'.",
                default=None,
            ),
        ] = None,
        platform: Annotated[
            str | None,
            Field(
                description="Resolver filter (unique_id mode only): restrict matches to this integration platform, e.g. 'hue'.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Get entity registry information for one or more entities.

        Returns detailed entity registry metadata: area, custom name/icon,
        original name, disabled_by/hidden_by (with enabled/hidden shorthands),
        aliases (a null entry = the entity's own name), labels, categories,
        device_class override, per-domain options, platform, device_id,
        config_entry_id and unique_id.

        RESOLVER MODE:
        Pass unique_id (instead of entity_id) to resolve a stable integration
        unique_id to its entity_id(s). Since the registry's unique key is
        (domain, platform, unique_id), the same unique_id can match multiple
        platforms — all matches are returned in entity_entries with a `matches`
        count. Narrow with domain/platform. Resolver reads as_partial_dict, so
        aliases and the device_class override come back as defaults ([]/null).

        When config_entry_id is non-null — e.g. for UI-created template / group /
        utility_meter / derivative helpers — pass it to
        ``ha_get_integration(entry_id=..., include_options=True)`` to read the
        helper's current config (template body, group members, etc.) without
        scanning a domain list. Voice-assistant exposure is stored under options
        but is set via ha_set_entity(expose_to=...).

        Resolved-name enrichment (present only when the ha_mcp_tools component
        advertises it; otherwise these keys are absent): `area` (assigned area
        NAME, device-inherited when the entity has none), `floor`, and
        `label_names`. This tool's base `labels` carries the label ids;
        ha_search result_fields and ha_get_entity_exposure instead emit the
        resolved names under `labels`.

        RELATED TOOLS: ha_set_entity() to modify these properties; ha_get_state()
        for current state/attributes; ha_search() to find entities.

        EXAMPLES:
        - Single entity: ha_get_entity("sensor.temperature")
        - Multiple entities: ha_get_entity(["light.living_room", "switch.porch"])
        - Resolve a unique_id: ha_get_entity(unique_id="00:11:22:33", domain="sensor")
        """
        try:
            if unique_id is not None:
                if entity_id is not None:
                    raise_failure(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            "Provide exactly one of entity_id or unique_id, not both.",
                            suggestions=[
                                "Pass unique_id alone to resolve it to entity_id(s)",
                                "Pass entity_id alone to look an entity up directly",
                            ],
                        )
                    )
                logger.info(f"Resolving unique_id '{unique_id}' to entity_id(s)")
                return await self._resolve_by_unique_id(unique_id, domain, platform)
            if entity_id is None:
                raise_failure(
                    create_error_response(
                        ErrorCode.VALIDATION_MISSING_PARAMETER,
                        "Provide entity_id (an id or list) or unique_id.",
                        suggestions=[
                            "ha_get_entity('sensor.temperature')",
                            "ha_get_entity(unique_id='abc123') to resolve a unique_id",
                        ],
                    )
                )
            if domain is not None or platform is not None:
                raise_failure(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "domain/platform are resolver filters — they only apply when unique_id is provided.",
                        suggestions=[
                            "Drop domain/platform, or pass unique_id to use them"
                        ],
                    )
                )
            return await self._get_by_entity_id(entity_id)
        except ToolFailure:
            raise
        except Exception as e:  # noqa: BLE001 - facade-specific error classification
            logger.error(f"Error getting entity: {e}")
            self._raise_entity_error(e, entity_id)

    async def _get_single_entity(self, eid: str) -> dict[str, Any]:
        """Look up one entity registry entry, with additive enrichment."""
        logger.info(f"Getting entity registry entry for {eid}")
        try:
            result = await self._fetch_entity(eid)
        except ValueError as e:
            raise_failure(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Entity not found: {e}",
                    context={"entity_id": eid},
                    suggestions=[
                        "Use ha_search() to find valid entity IDs",
                        "Check the entity_id spelling and format (e.g., 'sensor.temperature')",
                    ],
                )
            )
        enriched = await self._fetch_enrichment([eid])
        if enriched is not None:
            _merge_entity_enrichment(result, enriched.get(eid))
        return {"success": True, "entity_id": eid, "entity_entry": result}

    async def _get_by_entity_id(self, entity_id: str | list[str]) -> dict[str, Any]:
        """Look up entities by entity_id (single or bulk list)."""
        entity_ids, is_bulk, early_response = _parse_get_entity_ids(entity_id)
        if early_response is not None:
            return early_response
        if not is_bulk:
            return await self._get_single_entity(entity_ids[0])
        logger.info(f"Getting entity registry entries for {len(entity_ids)} entities")
        raw_map, error_map = await self._get_entries_raw(entity_ids)
        entity_entries: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for eid in entity_ids:
            raw = raw_map.get(eid)
            if raw is not None:
                entity_entries.append(_format_fetched_entity(raw))
            else:
                errors.append(
                    {"entity_id": eid, "error": error_map.get(eid, "Entity not found")}
                )
        found_ids: list[str] = [
            e["entity_id"]
            for e in entity_entries
            if isinstance(e.get("entity_id"), str)
        ]
        enriched = await self._fetch_enrichment(found_ids)
        if enriched is not None:
            for entry in entity_entries:
                entry_id = entry.get("entity_id")
                if isinstance(entry_id, str):
                    _merge_entity_enrichment(entry, enriched.get(entry_id))
        response: dict[str, Any] = {
            "success": True,
            "count": len(entity_entries),
            "entity_entries": entity_entries,
        }
        if errors:
            response["errors"] = errors
            response["suggestions"] = [
                "Use ha_search() to find valid entity IDs for failed lookups"
            ]
        return response
