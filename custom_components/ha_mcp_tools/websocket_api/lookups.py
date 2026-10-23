"""Single-object lookups: blueprint, device, enrichment, exposure and registry."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from ..blueprint_files import _read_blueprint_file as _read_blueprint_file
from ..core_device_reads import (
    _device_entities as _device_entities,
)
from ..core_device_reads import (
    _entries_for_device as _entries_for_device,
)
from ..core_device_reads import (
    device_get,
    device_list,
)
from ..core_entity_reads import (
    _all_exposable_entity_ids as _all_exposable_entity_ids,
)
from ..core_entity_reads import (
    _entity_exposed_to as _entity_exposed_to,
)
from ..core_entity_reads import (
    _exposure_enrichment as _exposure_enrichment,
)
from ..core_entity_reads import (
    _legacy_exposed_entity_ids as _legacy_exposed_entity_ids,
)
from ..core_entity_reads import (
    _registry_lookup_missing_target as _registry_lookup_missing_target,
)
from ..core_entity_reads import (
    enrich_entities as enrich_entities,
)
from ..core_entity_reads import (
    entity_exposure as entity_exposure,
)
from ..core_entity_reads import (
    registry_lookup as registry_lookup,
)
from .registry import (
    _all_entity_entries,
    _enum_value,
    _plainify,
    _resolve_registries,
    _substrate_unavailable,
)

# All ws_* modules log through the websocket_api logger, so one logger
# setting covers the whole command surface.
_LOGGER = logging.getLogger(__package__)


# =============================================================================
# ha_mcp_tools/blueprint_get
# =============================================================================
def _do_blueprint_get(
    hass: HomeAssistant,
    params: dict[str, Any],
    *,
    body: dict[str, Any] | None = None,
    text: str | None = None,
) -> dict[str, Any]:
    """Return one installed blueprint as ``{metadata, config, yaml}``.

    core's ``blueprint/list`` returns only ``{metadata}`` (no triggers /
    conditions / actions / sequence, and never the file text), so the server can
    otherwise serve metadata only. This reads the on-disk blueprint file once and
    returns both views of it: ``config`` is the parsed file (the server merges it
    additively over the ``blueprint/list`` metadata), ``metadata`` is its
    ``blueprint:`` section, and ``yaml`` is the raw text the server hands back for
    a round trip through ``blueprint/save``. Each is ``None`` when it could not be
    produced — a file that reads but does not parse still yields its ``yaml`` —
    and all three are ``None`` when the file is missing or the requested path
    escapes the jail (see :func:`_read_blueprint_file`).

    Pure: the blocking jail-resolve + file read + YAML parse run in the executor
    via :func:`_blueprint_get_prep`, which passes both views in.
    """
    if not isinstance(body, dict):
        return {"metadata": None, "config": None, "yaml": text}
    metadata = body.get("blueprint")
    return {
        "metadata": _plainify(metadata) if isinstance(metadata, dict) else None,
        "config": _plainify(body),
        "yaml": text,
    }


async def _blueprint_get_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    """Async pre-step for ``blueprint_get``: jail + read + parse off the loop.

    The path jail (symlink-safe ``Path.resolve`` containment), the ``open()`` and
    the YAML parse are all blocking filesystem work, so they run in the executor
    via :meth:`hass.async_add_executor_job` — keeping :func:`_do_blueprint_get` a
    pure assembler over the raw text and parsed body this returns (both ``None``
    on a failed read).
    """
    domain = msg["domain"]
    path = msg["path"]
    read = await hass.async_add_executor_job(_read_blueprint_file, hass, domain, path)
    return {"body": read.body, "text": read.text}


# =============================================================================
# ha_mcp_tools/device_get + ha_mcp_tools/device_list
# =============================================================================


# =============================================================================
# ha_mcp_tools/entity_enrich
# =============================================================================


# =============================================================================
# ha_mcp_tools/exposure
# =============================================================================


# =============================================================================
# ha_mcp_tools/registry_lookup
# =============================================================================


# =============================================================================
# ha_mcp_tools/entity_lookup
# =============================================================================
def _do_entity_lookup(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Return registry entries whose ``unique_id`` matches (domain/platform narrow).

    ``{matches: [{entity_id, unique_id, platform, domain, config_entry_id,
    categories, disabled_by, hidden_by}]}``. Scans the entity registry for every
    entry whose ``unique_id`` equals the requested one, optionally narrowed by
    ``domain`` (the entity's own domain, from its entity_id) and ``platform``
    (the owning integration). Multiple matches across platforms are all returned
    — the server picks. ``categories`` mirrors ``as_partial_dict``'s
    ``dict(entry.categories)``; ``disabled_by`` / ``hidden_by`` are unwrapped to
    their wire strings. In-process, so the read is authoritative immediately (no
    registry-write settle retry).

    A drifted entity registry (``er.async_get`` raised / renamed → ``None``) RAISES
    ``HomeAssistantError`` (→ server command-error fallback to the legacy scan)
    rather than returning ``{matches: []}`` — a well-formed empty the server can't
    tell from a genuine "no entry with that unique_id". A present-but-empty registry
    still returns ``{matches: []}`` (correct: no match).
    """
    unique_id = params.get("unique_id")
    domain = params.get("domain")
    platform = params.get("platform")
    view = _resolve_registries(hass)
    if view.entity is None:
        raise _substrate_unavailable("entity registry")
    matches: list[dict[str, Any]] = []
    for entry in _all_entity_entries(view):
        if getattr(entry, "unique_id", None) != unique_id:
            continue
        entity_id = getattr(entry, "entity_id", "") or ""
        ent_domain = entity_id.split(".")[0] if "." in entity_id else ""
        if domain and ent_domain != domain:
            continue
        if platform and getattr(entry, "platform", None) != platform:
            continue
        matches.append(
            {
                "entity_id": entity_id,
                "unique_id": getattr(entry, "unique_id", None),
                "platform": getattr(entry, "platform", None),
                "domain": ent_domain,
                "config_entry_id": getattr(entry, "config_entry_id", None),
                "categories": _plainify(getattr(entry, "categories", None) or {}),
                "disabled_by": _enum_value(getattr(entry, "disabled_by", None)),
                "hidden_by": _enum_value(getattr(entry, "hidden_by", None)),
            }
        )
    return {"matches": matches}


def _do_entity_enrich(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    return enrich_entities(hass, params.get("entity_ids") or [])


def _do_exposure(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    return entity_exposure(hass, params.get("entity_id"))


def _do_registry_lookup(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    return registry_lookup(
        hass, params.get("entity_ids"), params.get("config_entry_id")
    )


def _do_device_get(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    return device_get(
        hass, params.get("device_id"), params.get("include_entities", False)
    )


def _do_device_list(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    return device_list(hass)
