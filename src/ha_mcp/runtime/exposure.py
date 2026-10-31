"""Shared Assist exposure discovery and enrichment projections."""

from __future__ import annotations

from typing import Annotated, Any, NoReturn

from pydantic import Field

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tool_api import BackendTools, ToolFailure

from .errors import raise_failure

KNOWN_ASSISTANTS = ["conversation", "cloud.alexa", "cloud.google_assistant"]


_EXPOSURE_ENRICHMENT_KEYS = (
    "friendly_name",
    "domain",
    "area",
    "floor",
    "labels",
    "state",
)


class ExposureTools(BackendTools):
    async def _fetch_exposure_payload(self, entity_id: str | None) -> dict[str, Any]:
        arguments = {"entity_id": entity_id} if entity_id is not None else {}
        payload: dict[str, Any] = await self.backend.execute(
            "assist.exposure", arguments, self.context
        )
        return payload

    def _raise_exposure_error(
        self, error: Exception, entity_id: str | None
    ) -> NoReturn:
        raise_failure(
            create_error_response(
                ErrorCode.INTERNAL_ERROR,
                str(error),
                context={"entity_id": entity_id},
            )
        )

    @staticmethod
    def _merge_exposure_enrichment(
        response: dict[str, Any], info: dict[str, Any] | None
    ) -> None:
        """Additively merge one entity's ``entity_info`` onto its exposure response.

        Adds friendly_name / domain / area / floor / labels (and state, when the
        component included it) on top of the byte-identical legacy keys. A ``None``
        / empty ``info`` is a no-op (capability miss ⇒ fields simply absent).
        """
        if not info:
            return
        for key in _EXPOSURE_ENRICHMENT_KEYS:
            if key in info:
                response[key] = info[key]

    @staticmethod
    def _get_entity_exposure(
        entity_id: str, exposed_entities: dict[str, Any]
    ) -> dict[str, Any]:
        """Build response for a specific entity's exposure settings."""
        entity_settings = exposed_entities.get(entity_id, {})
        is_exposed = any(entity_settings.get(asst) for asst in KNOWN_ASSISTANTS)
        return {
            "success": True,
            "entity_id": entity_id,
            "exposed_to": {
                asst: entity_settings.get(asst, False) for asst in KNOWN_ASSISTANTS
            },
            "is_exposed_anywhere": is_exposed,
            "has_custom_settings": entity_id in exposed_entities,
            "note": (
                "If has_custom_settings is False, the entity uses default exposure settings"
                if entity_id not in exposed_entities
                else None
            ),
        }

    @staticmethod
    def _list_exposures(
        exposed_entities: dict[str, Any], assistant: str | None
    ) -> dict[str, Any]:
        """Build response listing all exposed entities with optional filter."""
        filtered = exposed_entities
        if assistant:
            filtered = {
                eid: settings
                for eid, settings in filtered.items()
                if settings.get(assistant)
            }

        summary: dict[str, int] = dict.fromkeys(KNOWN_ASSISTANTS, 0)
        for settings in filtered.values():
            for asst in KNOWN_ASSISTANTS:
                if settings.get(asst):
                    summary[asst] += 1

        filters_applied: dict[str, Any] = {}
        if assistant:
            filters_applied["assistant"] = assistant

        return {
            "success": True,
            "exposed_entities": filtered,
            "count": len(filtered),
            "total_entities_with_settings": len(exposed_entities),
            "summary": (
                summary if not assistant else {assistant: summary.get(assistant, 0)}
            ),
            "filters_applied": filters_applied,
        }

    async def ha_get_entity_exposure(
        self,
        entity_id: Annotated[
            str | None,
            Field(
                description="Entity ID to check exposure settings for.",
                default=None,
            ),
        ] = None,
        assistant: Annotated[
            str | None,
            Field(
                description=(
                    "Filter by assistant: 'conversation', 'cloud.alexa', or "
                    "'cloud.google_assistant'."
                ),
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Get entity exposure settings - list all or get settings for a specific entity.

        Without an entity_id: lists the entities that have explicit exposure
        settings for voice assistants (Alexa, Google Assistant, Assist) as
        `exposed_entities`, plus a per-assistant `summary` count. An entity absent
        from that list uses the default exposure settings.

        With an entity_id: returns `exposed_to` (assistant -> True/False from the
        entity's explicit settings), `is_exposed_anywhere`, and
        `has_custom_settings`. When has_custom_settings is False the defaults
        apply and exposed_to is all False; the default values themselves are
        not computed.

        EXAMPLES:
        - Filter by assistant: ha_get_entity_exposure(assistant="cloud.alexa")
        - Get specific entity: ha_get_entity_exposure(entity_id="light.living_room")

        When the ha_mcp_tools component advertises the exposure capability, each
        record is additively enriched with the entity's name/area so no second
        ha_search is needed to identify it: friendly_name, domain, area, floor,
        and labels (plus state for entities that have one) on a single-entity
        lookup, and a parallel entity_info map keyed by entity_id when listing.
        These fields are absent when the component is unavailable.
        """
        try:
            if assistant and assistant not in KNOWN_ASSISTANTS:
                raise_failure(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid assistant: {assistant}",
                        context={
                            "assistant": assistant,
                            "valid_assistants": KNOWN_ASSISTANTS,
                        },
                        suggestions=[
                            f"Valid assistants are: {', '.join(KNOWN_ASSISTANTS)}",
                            "Check the assistant parameter spelling",
                        ],
                    )
                )
            payload = await self._fetch_exposure_payload(entity_id)
            exposed_entities = payload.get("exposed_entities") or {}
            entity_info = payload.get("entity_info")
            if entity_id is not None:
                response = self._get_entity_exposure(entity_id, exposed_entities)
                self._merge_exposure_enrichment(
                    response, (entity_info or {}).get(entity_id)
                )
                return response
            response = self._list_exposures(exposed_entities, assistant)
            if entity_info is not None:
                response["entity_info"] = {
                    eid: entity_info[eid]
                    for eid in response["exposed_entities"]
                    if eid in entity_info
                }
            return response
        except ToolFailure:
            raise
        except Exception as error:  # noqa: BLE001 - facade-specific error classification
            self._raise_exposure_error(error, entity_id)
