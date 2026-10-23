"""WebSocket compatibility facade for Core registry snapshots."""

from typing import Any

from homeassistant.core import HomeAssistant

from ..core_registries import (
    _area_row as _area_row,
)
from ..core_registries import (
    _category_registry as _category_registry,
)
from ..core_registries import (
    _category_row as _category_row,
)
from ..core_registries import (
    _category_rows as _category_rows,
)
from ..core_registries import (
    _floor_row as _floor_row,
)
from ..core_registries import (
    _label_row as _label_row,
)
from ..core_registries import (
    _list_categories as _list_categories,
)
from ..core_registries import (
    _registries_missing_category_scopes as _registries_missing_category_scopes,
)
from ..core_registries import (
    registry_snapshot as registry_snapshot,
)


def _do_registries(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    return registry_snapshot(
        hass, params.get("registries") or [], params.get("category_scopes")
    )
