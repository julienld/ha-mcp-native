"""Compatibility imports; business owners live outside the WebSocket surface."""

from ..core_assist import (
    _assist_default_exposed,
    _assist_expose_new_entities,
    _assist_exposure_available,
    _assist_should_expose,
    _async_get_entity_settings,
    _explicit_assist_exposure,
    _is_unknown_entity_error,
)

__all__ = [
    "_assist_default_exposed",
    "_assist_expose_new_entities",
    "_assist_exposure_available",
    "_assist_should_expose",
    "_async_get_entity_settings",
    "_explicit_assist_exposure",
    "_is_unknown_entity_error",
]
