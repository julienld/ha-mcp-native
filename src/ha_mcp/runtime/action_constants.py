"""Shared service confirmation hints and policies, independent of transports."""

_STATE_CHANGING_SERVICES = {
    "turn_on",
    "turn_off",
    "toggle",
    "open",
    "close",
    "lock",
    "unlock",
    "set_temperature",
    "set_hvac_mode",
    "set_fan_mode",
    # fan.set_speed was removed in the HA percentage migration (gone in 2026.6);
    # its state-changing successors are set_percentage / set_preset_mode.
    "set_percentage",
    "set_preset_mode",
    "select_option",
    "set_value",
    "set_datetime",
    "set_cover_position",
    "set_position",
    "play_media",
    "media_play",
    "media_pause",
    "media_stop",
}


_NON_STATE_CHANGING_DOMAINS = {
    "automation",
    "script",
    "scene",
    "homeassistant",
    "notify",
    "tts",
    "persistent_notification",
    "logbook",
    "system_log",
}


_NON_ENVELOPE_WARNING = (
    "Home Assistant's return_response reply did not match the expected "
    "{changed_states, service_response} envelope, so no changed-state records "
    "could be separated from the response data. An empty 'result' here does NOT "
    "mean nothing changed — the whole reply is reported under 'service_response'."
)


_COMPONENT_RESPONSE_UNCONFIRMED_WARNING = (
    "The service was dispatched but its confirmation did not arrive, and no "
    "response data came back with it. A null 'service_response' here does NOT "
    "prove the service returned nothing — the response may have been produced "
    "and lost with the confirmation. Re-read state to confirm the outcome."
)


_SERVICE_TO_STATE: dict[str, str] = {
    "turn_on": "on",
    "turn_off": "off",
    "open": "open",
    "close": "closed",
    "lock": "locked",
    "unlock": "unlocked",
}


def is_single_entity_target(entity_id: str | None) -> bool:
    """True when ``entity_id`` looks like exactly one real, literal entity ID.

    False for two shapes a not-found/unavailable lookup miss cannot settle
    anything about:

    * A comma-separated multi-target list ("light.a,light.b") — a valid
      service-call payload the single-entity ``/api/states/<id>`` endpoint
      has no syntax for, so its 404 on the literal joined string proves
      nothing about the individual targets.
    * A Home Assistant magic broadcast target — ``ENTITY_MATCH_ALL`` ("all")
      or ``ENTITY_MATCH_NONE`` ("none"), both defined in
      ``homeassistant/const.py`` and neither a literal entity in the state
      machine, so a states-endpoint miss for either is expected and proves
      nothing about whether the underlying service call can succeed.

    A normal entity ID always has a ``domain.object_id`` shape (contains a
    dot); the absence of one is the general, defensive signal used here
    rather than hardcoding just "all"/"none" by name, so any other future
    magic target is covered the same way.
    """
    return entity_id is not None and "," not in entity_id and "." in entity_id


_SNAPSHOT_SERVICE_ACTIONS: dict[tuple[str, str], str] = {
    ("backup", "create"): "create",
    ("backup", "create_automatic"): "create",
    ("hassio", "backup_full"): "create",
    ("hassio", "backup_partial"): "create",
    ("hassio", "restore_full"): "restore",
    ("hassio", "restore_partial"): "restore",
}


__all__ = [
    "_COMPONENT_RESPONSE_UNCONFIRMED_WARNING",
    "_NON_ENVELOPE_WARNING",
    "_NON_STATE_CHANGING_DOMAINS",
    "_SERVICE_TO_STATE",
    "_SNAPSHOT_SERVICE_ACTIONS",
    "_STATE_CHANGING_SERVICES",
    "is_single_entity_target",
]
