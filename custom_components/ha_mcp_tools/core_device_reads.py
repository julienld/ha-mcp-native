"""Core device records and their entity joins shared by native and remote paths."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .core_registry import (
    _all_device_entries,
    _device_dict_repr,
    _effective_device_area_id,
    _entity_partial_dict,
    _RegistryView,
    _resolve_registries,
    _unambiguous_device_entries,
)

_LOGGER = logging.getLogger(__package__ + ".websocket_api")


def device_get(
    hass: HomeAssistant, device_id: str | None, include_entities: bool = False
) -> dict[str, Any]:
    """Return one device registry entry by id, optionally with its entities.

    ``{device: <DeviceEntry.dict_repr> | None}`` — a request-local snapshot over
    Core's supported main and child collections rejects conflicting identities,
    and the emitted body is core's ``DeviceEntry.dict_repr`` returned UNMODIFIED —
    exactly the shape
    ``config/device_registry/list`` serializes (it sends
    ``json_bytes(entry.dict_repr)``), so a component-served record is byte-identical
    to one legacy list element by construction (the WS transport JSON-encodes the
    same dict with the same encoder). The body is never ``_plainify``'d: that would
    ``str()`` the ``disabled_by`` / ``entry_type`` enums to their repr instead of the
    wire value core's encoder emits, breaking parity. ``device`` is ``None`` when no
    such device exists — the server maps that onto its own not-found contract.

    When ``include_entities`` is set, a SIBLING ``entities`` key carries the device's
    entity-registry rows (``[<RegistryEntry.as_partial_dict>, ...]`` — the same shape
    and serialization ``config/entity_registry/list`` emits), so a single-device
    lookup no longer pulls the WHOLE entity registry to list one device's entities.
    ``er.async_entries_for_device`` is called with ``include_disabled_entities=True``
    to match what ``config/entity_registry/list`` returns (it lists disabled entities
    too). The DeviceEntry dict itself stays exactly the raw shape — the join is a
    sibling, so consumers keep their own transforms. The ``entities`` key is present
    only when requested. A child-device result may also carry a sibling
    ``effective_area_id`` computed from its direct-or-parent placement; that
    transport-only field is absent from the raw ``device`` mapping.
    """
    view = _resolve_registries(hass)
    entry = _unambiguous_device_entries(view).get(device_id) if device_id else None
    entry_row = _device_dict_repr(entry) if entry is not None else None
    result: dict[str, Any] = {"device": entry_row}
    if (
        entry is not None
        and isinstance(entry_row, dict)
        and isinstance(entry_row.get("parent_device_id"), str)
    ):
        # Additive internal transport metadata. The raw child ``dict_repr`` stays
        # byte-identical to Core while the server can expose its existing area_id
        # field using Core's effective placement without a whole-registry read.
        result["effective_area_id"] = _effective_device_area_id(view, entry)
    if include_entities:
        result["entities"] = _device_entities(view, device_id) if device_id else []
    return result


def device_list(hass: HomeAssistant) -> dict[str, Any]:
    """Return every device registry entry as ``{devices: [dict_repr, ...]}``.

    The in-process equivalent of ``config/device_registry/list``: each element is
    core's ``DeviceEntry.dict_repr`` returned VERBATIM (same byte-parity rationale
    as :func:`_do_device_get`), so the server's existing device transforms consume
    it unchanged. An entry whose ``dict_repr`` is unavailable is skipped rather
    than emitted as a partial record.
    """
    view = _resolve_registries(hass)
    out: list[dict[str, Any]] = []
    for dev in _all_device_entries(view):
        repr_dict = _device_dict_repr(dev)
        if repr_dict is not None:
            out.append(repr_dict)
        else:
            _LOGGER.warning(
                "device_list: skipping device %r with unavailable dict_repr",
                getattr(dev, "id", None),
            )
    return {"devices": out}


def _device_entities(view: _RegistryView, device_id: str) -> list[dict[str, Any]]:
    """The device's entity-registry rows as ``config/entity_registry/list`` elements.

    Each row is core's ``RegistryEntry.as_partial_dict`` returned VERBATIM (the same
    shape + serialization ``config/entity_registry/list`` emits — it sends
    ``json_bytes(entry.partial_json_repr)`` over ``as_partial_dict``), so the
    server's device<->entity map builds identically off the join or the legacy list.
    A row whose ``as_partial_dict`` is unavailable is skipped.
    """
    out: list[dict[str, Any]] = []
    for entry in _entries_for_device(view, device_id):
        partial = _entity_partial_dict(entry)
        if partial is not None:
            out.append(partial)
    return out


def _entries_for_device(view: _RegistryView, device_id: str) -> list[Any]:
    """Entity-registry entries bound to ``device_id``, disabled ones INCLUDED.

    Delegates to core's ``er.async_entries_for_device`` (its device_id index) with
    ``include_disabled_entities=True`` so the result matches what
    ``config/entity_registry/list`` returns — that command lists disabled entities
    too, and dropping them would diverge the join from the legacy shape. Guarded
    against a missing registry / core drift (returns ``[]``).
    """
    reg = view.entity
    if reg is None or not device_id:
        return []
    try:
        entries = er.async_entries_for_device(
            reg, device_id, include_disabled_entities=True
        )
    except Exception:  # pragma: no cover - defensive; core drift  # noqa: BLE001
        return []
    return list(entries)
