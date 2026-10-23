"""WebSocket compatibility projection over Core config-entry readers."""

from typing import Any

from homeassistant.core import HomeAssistant

from ..core_config_entries import (
    _config_entry_by_id as _config_entry_by_id,
)
from ..core_config_entries import (
    _config_entry_row as _config_entry_row,
)
from ..core_config_entries import (
    _config_subentries as _config_subentries,
)
from ..core_config_entries import (
    _safe_prop as _safe_prop,
)
from ..core_config_entries import (
    load_entry_secret_values as load_entry_secret_values,
)
from ..core_config_entries import project_config_entries


async def _config_entries_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    return await load_entry_secret_values(hass)


def _do_config_entries(
    hass: HomeAssistant, params: dict[str, Any], **scrub: Any
) -> dict[str, Any]:
    return project_config_entries(hass, params, **scrub)
