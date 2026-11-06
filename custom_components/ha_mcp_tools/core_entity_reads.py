"""Core entity enrichment, exposure and registry lookups shared by transports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant

from .core_assist import _async_get_entity_settings, _is_unknown_entity_error
from .core_registry import (
    _all_entity_entries,
    _entity_partial_dict,
    _reg_entity,
    _registry_enrichment,
    _RegistryView,
    _resolve_registries,
    _state_get,
)


def enrich_entities(hass: HomeAssistant, entity_ids: list[str]) -> dict[str, Any]:
    """Return the area/floor/labels/aliases join for each requested entity_id.

    ``{entities: {id: {area, floor, labels, aliases}}}`` — each id runs through the
    SAME :func:`_registry_enrichment` join the search path uses (device-inherited
    area/labels, resolved NAMES), so ``ha_get_entity`` adds the resolved-name
    fields the raw registry entry lacks without the caller fanning out its own
    area/floor/label registry reads. Pure O(id) in-memory registry lookups; a
    registry-only (stateless) entity is enriched too (the join keys off the
    registry, not the state machine). An id with no registry entry yields empty /
    ``None`` fields rather than being dropped, so the caller can pair the result
    back to its request by key.
    """
    view = _resolve_registries(hass)
    entities: dict[str, Any] = {}
    for entity_id in entity_ids:
        join = _registry_enrichment(view, entity_id)
        entities[entity_id] = {
            "area": join["area"],
            "floor": join["floor"],
            "labels": join["labels"],
            "aliases": join["aliases"],
        }
    return {"entities": entities}


def entity_exposure(
    hass: HomeAssistant, entity_id: str | None = None
) -> dict[str, Any]:
    """Return voice-assistant exposure with names/areas attached.

    ``{exposed_entities: {id: {assistant: True}}, entity_info: {id: {...}}}``.
    ``exposed_entities`` is byte-identical to core's ``ws_list_exposed_entities``
    result (``homeassistant/expose_entity/list``): only ``should_expose``-true
    assistants appear, and an entity with none is omitted from the map — so the
    server's existing exposure shaping consumes it unchanged. ``entity_info`` is
    the additive half: each relevant id enriched through :func:`_registry_enrichment`
    (friendly_name/domain/area/floor/labels), closing the "one call gives a bare
    ``{id: {assistant: bool}}`` map with no names/areas" gap.

    Modes:

    * single-entity (``entity_id`` set) — reads core's module-level
      ``async_get_entity_settings`` for that id and enriches it (whether exposed or
      not — the caller asked about that specific entity).
    * list (``entity_id`` omitted) — mirrors ``ws_list_exposed_entities``: walks
      the exposed-entities store ids + the entity registry, keeps the exposed ones,
      and enriches each.

    Parity guardrails (mirroring the legacy shape, pinned in tests):

    1. only ``should_expose``-true assistants are reported (the raw helper returns
       every assistant that has *any* stored option, not just exposed ones);
    2. core's ``HomeAssistantError("Unknown entity")`` on a junk id is caught and
       degrades to the not-exposed default (the legacy ``expose_entity/list`` never
       raises on a junk id);
    3. a missing ``hass.states.get(id)`` omits the live-state fields
       (friendly_name / state) from ``entity_info`` rather than crashing.
    """
    view = _resolve_registries(hass)

    if entity_id:
        exposed_to = _entity_exposed_to(hass, entity_id)
        return {
            "exposed_entities": {entity_id: exposed_to} if exposed_to else {},
            "entity_info": {entity_id: _exposure_enrichment(hass, view, entity_id)},
        }

    exposed_entities: dict[str, Any] = {}
    entity_info: dict[str, Any] = {}
    for eid in _all_exposable_entity_ids(hass, view):
        exposed_to = _entity_exposed_to(hass, eid)
        if not exposed_to:
            continue
        exposed_entities[eid] = exposed_to
        entity_info[eid] = _exposure_enrichment(hass, view, eid)
    return {"exposed_entities": exposed_entities, "entity_info": entity_info}


def _entity_exposed_to(hass: HomeAssistant, entity_id: str) -> dict[str, bool]:
    """``{assistant: True}`` for the entity's ``should_expose``-true assistants.

    Reads core's ``async_get_entity_settings`` (via the local
    :func:`_async_get_entity_settings` test-seam wrapper) and keeps only assistants
    whose settings carry a truthy ``should_expose`` (guardrail 1 — the raw helper is
    not pre-filtered like ``ws_list_exposed_entities``). A junk id whose helper raises
    ``HomeAssistantError("Unknown entity")`` degrades to ``{}`` (guardrail 2), the
    same not-exposed default the legacy path returns for an id it never listed.
    """
    try:
        settings = _async_get_entity_settings(hass, entity_id)
    except Exception as exc:
        if _is_unknown_entity_error(exc):
            return {}
        raise
    out: dict[str, bool] = {}
    if isinstance(settings, Mapping):
        for assistant, opts in settings.items():
            if isinstance(opts, Mapping) and opts.get("should_expose"):
                out[str(assistant)] = True
    return out


def _exposure_enrichment(
    hass: HomeAssistant, view: _RegistryView, entity_id: str
) -> dict[str, Any]:
    """area/floor/labels + domain for an id, plus live-state fields when present.

    Runs the id through :func:`_registry_enrichment` for area/floor/labels
    (device-inherited names). ``domain`` comes from the id itself (no state
    needed). ``friendly_name`` and ``state`` are LIVE-STATE fields: included only
    when ``hass.states.get(id)`` exists, omitted otherwise (guardrail 3 — a
    disabled / legacy-only entity has no state, so those keys are simply absent
    rather than crashing the join).
    """
    join = _registry_enrichment(view, entity_id)
    domain = entity_id.split(".", maxsplit=1)[0] if "." in entity_id else ""
    info: dict[str, Any] = {
        "domain": domain,
        "area": join["area"],
        "floor": join["floor"],
        "labels": join["labels"],
    }
    state = _state_get(hass, entity_id)
    if state is not None:
        attrs = getattr(state, "attributes", None) or {}
        friendly = (
            attrs.get("friendly_name", entity_id)
            if isinstance(attrs, Mapping)
            else entity_id
        )
        info["friendly_name"] = str(friendly)
        info["state"] = getattr(state, "state", "unknown")
    return info


def _all_exposable_entity_ids(hass: HomeAssistant, view: _RegistryView) -> list[str]:
    """Every id ``ws_list_exposed_entities`` walks: store ids plus registry ids.

    Core iterates ``chain(exposed_entities.entities, entity_registry.entities)`` —
    the legacy store (entities WITHOUT a unique_id, exposed manually) plus every
    registry entity. This reproduces that union, de-duplicated with store-first
    order, so an exposed YAML entity that lives only in the store is not missed.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for eid in _legacy_exposed_entity_ids(hass):
        if eid and eid not in seen:
            seen.add(eid)
            ordered.append(eid)
    for entry in _all_entity_entries(view):
        entry_eid = getattr(entry, "entity_id", None)
        if entry_eid and entry_eid not in seen:
            seen.add(entry_eid)
            ordered.append(entry_eid)
    return ordered


def _legacy_exposed_entity_ids(hass: HomeAssistant) -> list[str]:
    """Entity ids in the exposed-entities store (entities without a unique_id).

    The ``exposed_entities.entities`` half of core's ``ws_list_exposed_entities``
    iteration. Imported lazily (test seam); a missing store / core drift yields
    ``[]`` so list mode still enumerates the registry half.
    """
    try:
        from homeassistant.components.homeassistant.const import (
            DATA_EXPOSED_ENTITIES,
        )

        data = getattr(hass, "data", None)
        store = data.get(DATA_EXPOSED_ENTITIES) if isinstance(data, Mapping) else None
    except Exception:  # pragma: no cover - defensive; core drift  # noqa: BLE001
        return []
    entities = getattr(store, "entities", None)
    if isinstance(entities, Mapping):
        return [str(eid) for eid in entities]
    return []


def registry_lookup(
    hass: HomeAssistant,
    entity_ids: list[str] | None = None,
    config_entry_id: str | None = None,
) -> dict[str, Any]:
    """Return entity-registry rows for a set of ids or one config entry.

    Rows are core's ``RegistryEntry.as_partial_dict`` VERBATIM (the
    ``config/entity_registry/list`` shape, disabled entities included), so the
    consumers that parse that exact shape today read the component-served rows
    unchanged.

    * ``config_entry_id`` — scans the whole entity registry and returns EVERY
      entity bound to that entry (``{entities: [...]}``). It deliberately does
      NOT reuse :func:`_entities_by_config_entry` (single-valued — first entity
      only), so a multi-entity flow helper (a utility_meter and its tariff
      sub-entities) does not silently lose members.
    * ``entity_ids`` — looks each up (``{entities: [...], missing: [...]}``); an
      id with no registry entry lands in ``missing`` rather than being dropped.

    Pure O(n)/O(id) in-memory registry reads. Exactly one of the two params is
    meaningful — the schema rejects both being present; neither present (or only an
    empty-string ``config_entry_id`` / empty ``entity_ids`` list — no usable target)
    raises ``HomeAssistantError`` (see :func:`_registry_lookup_missing_target`)
    rather than silently returning an empty result the caller could mistake for "no
    matches".
    """
    entity_ids = entity_ids or []
    if not config_entry_id and not entity_ids:
        raise _registry_lookup_missing_target()

    view = _resolve_registries(hass)
    if config_entry_id:
        rows = [
            _entity_partial_dict(entry)
            for entry in _all_entity_entries(view)
            if getattr(entry, "config_entry_id", None) == config_entry_id
        ]
        return {"entities": [row for row in rows if row is not None]}

    found: list[dict[str, Any]] = []
    missing: list[str] = []
    for entity_id in entity_ids:
        entry = _reg_entity(view, entity_id)
        row = _entity_partial_dict(entry) if entry is not None else None
        if row is None:
            missing.append(entity_id)
        else:
            found.append(row)
    return {"entities": found, "missing": missing}


def _registry_lookup_missing_target() -> Exception:
    """Build a ``HomeAssistantError`` for a target-less ``registry_lookup`` request.

    Mirrors :func:`_backup_unavailable`: imported function-locally (test-stubbable)
    so a request with no usable target — neither ``entity_ids`` nor
    ``config_entry_id``, OR only an empty-string / empty-list one — raises instead of
    returning ``{entities: [], missing: []}``, a shape indistinguishable from a
    genuine "nothing matched" result, letting the server's command-error fallback
    fire for the degenerate case. (This is a deliberate divergence from
    ``config_entries``, whose empty-string ``entry_id`` is an authoritative empty
    result mirroring ``async_get_entry("")`` — a different, intentional semantic.)
    """
    from homeassistant.exceptions import HomeAssistantError

    err: Exception = HomeAssistantError(
        "ha_mcp_tools/registry_lookup requires a non-empty entity_ids or "
        "config_entry_id"
    )
    return err
