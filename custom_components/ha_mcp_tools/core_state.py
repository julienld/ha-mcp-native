"""Small live Core state and service-name readers shared by all transports."""

from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant

from .core_registry import _call_no_arg, _state_as_dict, _state_get


def service_names(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Service catalog in the ``client.get_services()`` list shape.

    The consumer's ``_build_service_stats`` reads only the per-domain service
    *names*, so each service maps to an empty dict — keeps the frame small while
    preserving the ``{domain, services: {name: {...}}}`` structure.
    """
    services = _call_no_arg(getattr(hass, "services", None), "async_services")
    if not isinstance(services, Mapping):
        return []
    out: list[dict[str, Any]] = []
    for domain, svcs in services.items():
        names = list(svcs.keys()) if isinstance(svcs, Mapping) else []
        out.append({"domain": domain, "services": {name: {} for name in names}})
    return out


def read_states(hass: HomeAssistant, entity_ids: list[str]) -> dict[str, Any]:
    """Return ``State.as_dict()`` for each requested entity_id + a ``missing`` list.

    ``hass.states.get(id)`` is a pure O(1) in-memory dict read, and core's
    ``State.as_dict()`` is exactly the serialization the REST ``/api/states/<id>``
    endpoint emits — so a component-served record is byte-identical to the legacy
    per-id REST fetch by construction (the WS transport JSON-encodes the same
    datetimes to the same ISO strings the REST layer does). The body is returned
    UNMODIFIED — never ``_plainify``'d — precisely so that byte-parity holds:
    ``_plainify``'s ``str()`` would render a datetime with a space separator where
    both REST and WS use ``isoformat``'s ``T``. No freshness or secrets concern:
    state bodies are always live and carry no ``!secret`` plaintext. The server
    enforces its own ``MAX_ENTITIES`` cap before calling, so no per-frame guard is
    needed here (100 full states is well within one frame — ``overview`` already
    returns every state in one call).
    """
    states: dict[str, Any] = {}
    missing: list[str] = []
    for entity_id in entity_ids:
        state = _state_get(hass, entity_id)
        if state is None:
            missing.append(entity_id)
            continue
        as_dict = _state_as_dict(state)
        if as_dict is None:
            # A live state that could not be serialized (core drift) goes to
            # ``missing`` rather than emitting a null state indistinguishable from
            # a real value — the server maps ``missing`` onto its per-id contract.
            missing.append(entity_id)
            continue
        states[entity_id] = as_dict
    return {"states": states, "missing": missing}
