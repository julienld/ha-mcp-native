"""Legacy message names retain their historical transport envelope."""

from typing import Any

from ..backends.legacy import legacy_errors_sync
from ..runtime.area_payloads import (
    build_area_create_payload,
    build_area_update_payload,
    build_floor_create_payload,
    build_floor_update_payload,
)
from ..runtime.area_payloads import resolve_clearable as resolve_clearable
from ..runtime.area_payloads import (
    validate_cross_kind_params as _validate_cross_kind_params,
)

validate_cross_kind_params = legacy_errors_sync(_validate_cross_kind_params)


def build_area_create_message(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return {
        "type": "config/area_registry/create",
        **build_area_create_payload(*args, **kwargs),
    }


def build_area_update_message(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return {
        "type": "config/area_registry/update",
        **build_area_update_payload(*args, **kwargs),
    }


def build_floor_create_message(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return {
        "type": "config/floor_registry/create",
        **build_floor_create_payload(*args, **kwargs),
    }


def build_floor_update_message(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return {
        "type": "config/floor_registry/update",
        **build_floor_update_payload(*args, **kwargs),
    }
