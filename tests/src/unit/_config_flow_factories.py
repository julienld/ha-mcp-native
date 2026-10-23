"""Framework-shaped flow instances without loading a Home Assistant runtime."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock


def options_flow(module: Any, *, options: Any = None, data: Any = None) -> Any:
    flow = module.HaMcpServerOptionsFlow()
    flow.config_entry = SimpleNamespace(
        options=options or {},
        data=data or {},
        version=module.HaMcpToolsConfigFlow.VERSION,
        minor_version=module.HaMcpToolsConfigFlow.MINOR_VERSION,
    )
    # No credentials are bound unless the test explicitly supplies them.
    flow.hass = MagicMock(name="hass")
    flow.hass.data = {}
    flow.async_show_form = MagicMock(
        side_effect=lambda **kwargs: {"type": "form", **kwargs}
    )
    flow.async_create_entry = MagicMock(
        side_effect=lambda **kwargs: {"type": "entry", **kwargs}
    )
    return flow
