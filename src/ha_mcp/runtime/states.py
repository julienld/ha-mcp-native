"""Shared state tool orchestration; only data access varies by backend."""

import logging
from typing import Annotated, Any

from pydantic import Field

from ha_mcp.errors import create_entity_not_found_error, create_validation_error
from ha_mcp.tool_api import Backend, CallContext
from ha_mcp.tools.coercion import JSON_STRING_COERCION, parse_string_list_param

from .errors import raise_failure
from .projection import project_entity_record as _project_entity
from .projection import timezone_metadata

logger = logging.getLogger(__name__)


def _accumulate_state_results(
    unique_ids: list[str],
    results: list[dict[str, Any]],
    parsed_fields: list[str] | None,
    parsed_attribute_keys: list[str] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Process asyncio.gather results into (states dict, errors list, attr_warnings list)."""
    states: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    attr_warns: list[str] = []
    for eid, result in zip(unique_ids, results, strict=True):
        if result.get("success") is True and "state" in result:
            state_record, attr_warn = _project_entity(
                result["state"], parsed_fields, parsed_attribute_keys
            )
            states[eid] = state_record
            if attr_warn and attr_warn not in attr_warns:
                attr_warns.append(attr_warn)
        else:
            error_detail = result.get("error")
            if error_detail is None:
                error_detail = {"code": "INTERNAL_ERROR", "message": "Unknown error"}
            errors.append(
                {
                    "entity_id": result.get("entity_id", eid),
                    "error": error_detail,
                }
            )
    return states, errors, attr_warns


def _build_bulk_states_response(
    states: dict[str, Any],
    errors: list[dict[str, Any]],
    attr_warns: list[str],
    attribute_keys_no_effect: bool,
) -> dict[str, Any]:
    """Build the bulk-state response dict from accumulated states, errors, and warnings."""
    response: dict[str, Any] = {
        "success": len(states) > 0,
        "count": len(states),
        "states": states,
    }
    if attribute_keys_no_effect:
        response.setdefault("warnings", []).append(
            "attribute_keys was ignored because 'attributes' is not in "
            "fields=. Add 'attributes' to fields= (or omit fields=) to "
            "apply attribute_keys."
        )
    for _w in attr_warns:
        response.setdefault("warnings", []).append(_w)
    if errors:
        response["errors"] = errors
        response["error_count"] = len(errors)
        response["suggestions"] = [
            "Use ha_search() to find correct entity IDs for failed lookups",
            "Verify entities exist in Home Assistant",
        ]
        if states:
            response["partial"] = True
    return response


class StateTools:
    def __init__(self, backend: Backend, context: CallContext) -> None:
        self.backend = backend
        self.context = context

    async def _timezone(self, data: dict[str, Any]) -> dict[str, Any]:
        result = await self.backend.execute("instance.timezone", {}, self.context)
        return timezone_metadata(
            data, result["timezone"], result.get("fetch_failed", False)
        )

    async def _get_one_state(self, entity_id: str) -> dict[str, Any]:
        result = await self.backend.execute(
            "states.read", {"entity_ids": [entity_id]}, self.context
        )
        if entity_id not in result["states"]:
            raise_failure(
                result.get("errors", {}).get(entity_id, _missing_entity(entity_id))
            )
        return result["states"][entity_id]

    async def _resolve_bulk_state_results(
        self, entity_ids: list[str]
    ) -> list[dict[str, Any]]:
        result = await self.backend.execute(
            "states.read", {"entity_ids": entity_ids}, self.context
        )
        return [
            {"success": True, "entity_id": eid, "state": result["states"][eid]}
            if eid in result["states"]
            else result.get("errors", {}).get(eid, _missing_entity(eid))
            for eid in entity_ids
        ]

    async def ha_get_state(
        self,
        entity_id: Annotated[
            str | list[str],
            JSON_STRING_COERCION,
            Field(
                description="Entity ID or list of entity IDs to retrieve state for "
                "(e.g., 'light.kitchen' or ['light.kitchen', 'sensor.temperature'])"
            ),
        ],
        fields: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "Return only the specified top-level entity record keys to reduce "
                    'response size (e.g. ["state", "attributes"]). None = full entity '
                    "record. Available keys: entity_id, state, attributes, last_changed, "
                    "last_reported, last_updated, context."
                ),
            ),
        ] = None,
        attribute_keys: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "Return only the specified keys from each entity's attributes dict "
                    '(e.g. ["brightness", "color_temp_kelvin"] for lights). None = full '
                    "attributes. Unknown keys are silently dropped."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Get current status, state, and attributes of one or more entities (lights, switches, sensors, climate, covers, locks, fans, etc.).

        Pass a string entity_id for one entity, or a list (max 100, duplicates
        deduplicated) for several, fetched in parallel. A bulk call returns
        success=True if at least one state was retrieved; check 'error_count'
        for failed lookups.

        `fields=` projects the per-entity record keys, NOT the outer bulk
        response wrapper: in single-entity mode it filters the returned record;
        in bulk mode it filters each record inside `states[entity_id]` while
        outer keys (`success`, `count`, `states`, `errors`, ...) are always
        preserved. `attribute_keys=` further narrows the `attributes` sub-dict
        and is only applied when `"attributes"` is in `fields=` (or
        `fields=None`); otherwise it is a no-op and a `warnings` list is emitted
        outside the projected record(s) — at the response wrapper level in bulk
        mode, at the top-level result (sibling of `data`/`metadata`) in
        single-entity mode — so `fields=["state"]` still returns a record with
        only `state`.

        EXAMPLES:
        - Single: ha_get_state("light.kitchen")
        - Multiple: ha_get_state(["light.kitchen", "light.living_room", "sensor.temperature"])
        - Slim bulk: ha_get_state(["light.kitchen", "sensor.temperature"], fields=["state", "attributes"], attribute_keys=["brightness"])
        """
        # Parse projection params once up front so the bulk loop doesn't re-parse
        # the same string/CSV input per entity.
        try:
            parsed_fields = parse_string_list_param(fields, "fields", allow_csv=True)
        except ValueError as e:
            raise_failure(create_validation_error(str(e), parameter="fields"))
        try:
            parsed_attribute_keys = parse_string_list_param(
                attribute_keys, "attribute_keys", allow_csv=True
            )
        except ValueError as e:
            raise_failure(create_validation_error(str(e), parameter="attribute_keys"))

        # `attribute_keys` only takes effect when `attributes` is in the projected
        # field set (or `fields=None`). Surface a warning rather than silently
        # ignoring it.
        attribute_keys_no_effect = (
            parsed_attribute_keys is not None
            and parsed_fields is not None
            and "attributes" not in parsed_fields
        )

        if isinstance(entity_id, str):
            return await self._get_single_entity_state(
                entity_id,
                parsed_fields,
                parsed_attribute_keys,
                attribute_keys_no_effect,
            )
        return await self._get_bulk_entity_states(
            entity_id, parsed_fields, parsed_attribute_keys, attribute_keys_no_effect
        )

    async def _get_single_entity_state(
        self,
        entity_id: str,
        parsed_fields: list[str] | None,
        parsed_attribute_keys: list[str] | None,
        attribute_keys_no_effect: bool,
    ) -> dict[str, Any]:
        """Fetch and return state for a single entity ID."""
        result = await self._get_one_state(entity_id)
        entity_record, attr_warn = _project_entity(
            result, parsed_fields, parsed_attribute_keys
        )
        # Always wrap (include_metadata=True); callers and tests rely on
        # the ``result["data"]`` envelope even when fields= is active.
        wrapped = await self._timezone(entity_record)
        # ``attribute_keys`` was specified but ``attributes`` is not in the
        # projected ``fields=`` set. Attach the warning at the outer wrapper
        # level (sibling of ``data``/``metadata``) — the FIELDS PROJECTION
        # contract: ``fields=`` filters the keys of the returned record;
        # ``warnings`` is not a record key.
        if attribute_keys_no_effect:
            wrapped.setdefault("warnings", []).append(
                "attribute_keys was ignored because 'attributes' is not in "
                "fields=. Add 'attributes' to fields= (or omit fields=) to "
                "apply attribute_keys."
            )
        if attr_warn:
            wrapped.setdefault("warnings", []).append(attr_warn)
        return wrapped

    async def _get_bulk_entity_states(
        self,
        entity_ids: list[str],
        parsed_fields: list[str] | None,
        parsed_attribute_keys: list[str] | None,
        attribute_keys_no_effect: bool,
    ) -> dict[str, Any]:
        """Fetch states for multiple entity IDs in parallel."""
        MAX_ENTITIES = 100

        if not isinstance(entity_ids, list) or not entity_ids:
            raise_failure(
                create_validation_error(
                    "entity_id must be a non-empty string or list of entity ID strings",
                    parameter="entity_id",
                )
            )

        if not all(isinstance(eid, str) for eid in entity_ids):
            raise_failure(
                create_validation_error(
                    "All entity_id values must be strings",
                    parameter="entity_id",
                )
            )

        if len(entity_ids) > MAX_ENTITIES:
            raise_failure(
                create_validation_error(
                    f"Too many entity IDs: {len(entity_ids)} exceeds maximum of {MAX_ENTITIES}",
                    parameter="entity_id",
                )
            )

        # Deduplicate while preserving order
        unique_ids = list(dict.fromkeys(entity_ids))
        if len(unique_ids) < len(entity_ids):
            logger.debug(
                f"Deduplicated entity_ids: {len(entity_ids)} -> {len(unique_ids)}"
            )

        results = await self._resolve_bulk_state_results(unique_ids)
        states, errors, attr_warns = _accumulate_state_results(
            unique_ids, results, parsed_fields, parsed_attribute_keys
        )
        response = _build_bulk_states_response(
            states, errors, attr_warns, attribute_keys_no_effect
        )
        return await self._timezone(response)


def _missing_entity(entity_id: str) -> dict[str, Any]:
    return create_entity_not_found_error(
        entity_id, details=f"API error: 404 - Entity {entity_id} not found"
    )
