"""Native policy choices reuse the remote projection without a client emulator."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools import ui_panel  # noqa: E402
from custom_components.ha_mcp_tools.native_settings import NativeSettings  # noqa: E402
from ha_mcp.policy import value_sources  # noqa: E402


@pytest.mark.parametrize(
    "source, data, params, expected",
    [
        (
            "ha_domains",
            [{"domain": "switch", "services": {}}, {"domain": "light", "services": {}}],
            {},
            ["light", "switch"],
        ),
        (
            "ha_services",
            {"light": {"toggle": {}, "turn_on": {}}, "switch": {"turn_off": {}}},
            {"domain": "light"},
            ["toggle", "turn_on"],
        ),
        (
            "ha_entities",
            [
                {"entity_id": "switch.a"},
                {"entity_id": "light.b"},
                {"entity_id": "light.a"},
            ],
            {"domain": "light"},
            ["light.a", "light.b"],
        ),
        (
            "ha_config_entries",
            [{"entry_id": "second", "future": 1}, {"entry_id": "first"}],
            {},
            ["first", "second"],
        ),
    ],
)
async def test_native_and_remote_choice_projection_matches(
    source, data, params, expected, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(value_sources, "_cache", {})
    client = SimpleNamespace(
        get_services=AsyncMock(return_value=data),
        get_states=AsyncMock(return_value=data),
        _request=AsyncMock(return_value=data),
    )
    assert (
        await value_sources.fetch_value_source(source, client=client, params=params)
        == expected
    )
    assert value_sources.project_value_source(source, data, params) == expected


async def test_native_choice_read_revalidates_administrator_before_core_access(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hass = SimpleNamespace(
        config=SimpleNamespace(path=lambda _subdir: str(tmp_path)),
        states=SimpleNamespace(async_all=MagicMock()),
    )
    ui = NativeSettings(SimpleNamespace(hass=hass))
    monkeypatch.setattr(
        ui_panel, "_session_user_is_admin", AsyncMock(return_value=False)
    )
    request = SimpleNamespace(query={"source": "ha_entities"}, cookies={})
    result = await ui._value_source(request)
    assert result.status == 401
    hass.states.async_all.assert_not_called()
