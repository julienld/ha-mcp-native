"""Parameter contracts shared by native owners and the WS compatibility facade."""

from typing import Any

CALL_SERVICE_DEFAULT_TIMEOUT = 10.0
CALL_SERVICE_MAX_TIMEOUT = 60.0
REGISTRY_KINDS = ("area", "floor", "label", "category")


def states_parameters(vol: Any) -> dict[Any, Any]:
    return {
        vol.Required("entity_ids"): [str],
    }


def entity_enrich_parameters(vol: Any) -> dict[Any, Any]:
    return {
        vol.Required("entity_ids"): [str],
    }


def exposure_parameters(vol: Any) -> dict[Any, Any]:
    return {
        vol.Optional("entity_id"): vol.Any(str, None),
    }


def config_entries_parameters(vol: Any) -> dict[Any, Any]:
    return {
        vol.Optional("entry_id"): vol.Any(str, None),
        vol.Optional("domain"): vol.Any(str, None),
        vol.Optional("include_subentry_data", default=False): bool,
    }


def registry_lookup_parameters(vol: Any) -> dict[Any, Any]:
    # Exactly one of entity_ids / config_entry_id is meaningful; ``vol.Exclusive``
    # rejects a request carrying BOTH (the two share the ``target`` group).
    # Voluptuous has no clean way to express "at least one of" in a flat schema,
    # so a request with NEITHER present is caught in ``_do_registry_lookup``
    # instead (raises ``HomeAssistantError`` rather than a silent empty result).
    return {
        vol.Exclusive("entity_ids", "target"): [str],
        vol.Exclusive("config_entry_id", "target"): str,
    }


def registries_parameters(vol: Any) -> dict[Any, Any]:
    return {
        vol.Required("registries"): [vol.In(REGISTRY_KINDS)],
        vol.Optional("category_scopes"): [str],
    }


def services_list_parameters(vol: Any) -> dict[Any, Any]:
    return {
        vol.Optional("domain"): vol.Any(str, None),
        vol.Optional("language", default="en"): str,
    }


def reference_data_parameters(vol: Any) -> dict[Any, Any]:
    return {
        vol.Optional("include_states", default=True): bool,
    }


def call_service_parameters(vol: Any) -> dict[Any, Any]:
    # ``entity_ids`` is the set of targets to CONFIRM (the pre/post transition is
    # built for these), not the service target itself — a caller may pass an empty
    # list for a non-entity service (``automation.trigger`` etc.) and still get the
    # dispatch result. ``timeout`` is capped at ``CALL_SERVICE_MAX_TIMEOUT`` so a
    # single write frame cannot park the connection. Mutable defaults use the
    # callable form so each validation produces a fresh ``{}`` / ``[]``.
    return {
        vol.Required("domain"): str,
        vol.Required("service"): str,
        vol.Optional("service_data", default=dict): dict,
        vol.Optional("entity_ids", default=list): [str],
        vol.Optional("wait", default=True): bool,
        vol.Optional("timeout", default=CALL_SERVICE_DEFAULT_TIMEOUT): vol.All(
            vol.Any(int, float), vol.Range(min=0, max=CALL_SERVICE_MAX_TIMEOUT)
        ),
        vol.Optional("return_response", default=False): bool,
        # Optional confirmation HINT (the server's ``_SERVICE_TO_STATE.get(service)``,
        # or None): the expected primary state the waiter confirms on REACHING —
        # skipping a multi-phase service's intermediate states and attribute-only
        # noise — and immediate-matches for an idempotent no-op. Optional/default-None
        # so a server that does not send it (or a non-mapped service) keeps today's
        # any-first-event confirmation. It governs confirmation TIMING only; the
        # returned transition is always the REAL observed one.
        vol.Optional("expected_state"): vol.Any(str, None),
    }


def helper_item_parameters(vol: Any) -> dict[Any, Any]:
    from .helper_collections import SIMPLE_HELPER_TYPES

    return {
        vol.Required("helper_type"): vol.In(SIMPLE_HELPER_TYPES),
        vol.Exclusive("entity_id", "target"): str,
        vol.Exclusive("item_id", "target"): str,
    }


def helper_write_parameters(vol: Any) -> dict[Any, Any]:
    from .helper_collections import SIMPLE_HELPER_TYPES

    return {
        vol.Required("helper_type"): vol.In(SIMPLE_HELPER_TYPES),
        vol.Required("action"): vol.In(("create", "update")),
        vol.Optional("item_id"): str,
        vol.Required("data"): dict,
        vol.Optional("registry"): {
            vol.Optional("icon"): vol.Any(str, None),
            vol.Optional("area_id"): vol.Any(str, None),
            vol.Optional("labels"): [str],
            vol.Optional("category"): vol.Any(str, None),
        },
    }
