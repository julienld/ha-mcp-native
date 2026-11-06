"""Panel operations keep the validated session actor through awaited work."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ._embedded_stubs import install

install()


async def test_panel_context_is_cookie_bound_and_revalidates_admin(monkeypatch):
    from custom_components.ha_mcp_tools import ui_panel
    from custom_components.ha_mcp_tools.native_settings_context import settings_context

    monkeypatch.setattr(ui_panel, "time", SimpleNamespace(monotonic=lambda: 100))
    user = SimpleNamespace(
        id="alice", is_active=True, is_admin=True, system_generated=False
    )
    hass = SimpleNamespace(
        data={}, auth=SimpleNamespace(async_get_user=AsyncMock(return_value=user))
    )
    token = ui_panel._mint_session(hass, "alice")
    request = SimpleNamespace(cookies={ui_panel._COOKIE_NAME: token})
    actor = await settings_context(hass, request)
    assert actor.user_id == "alice" and token not in repr(actor)
    assert actor.credential is not None
    await actor.credential()
    user.is_admin = False
    with pytest.raises(PermissionError, match="session"):
        await actor.credential()


async def test_panel_context_cannot_rebind_to_another_session_owner(monkeypatch):
    from custom_components.ha_mcp_tools import ui_panel
    from custom_components.ha_mcp_tools.native_settings_context import settings_context

    monkeypatch.setattr(ui_panel, "time", SimpleNamespace(monotonic=lambda: 100))
    hass = SimpleNamespace(
        data={},
        auth=SimpleNamespace(
            async_get_user=AsyncMock(
                return_value=SimpleNamespace(
                    is_active=True, is_admin=True, system_generated=False
                )
            )
        ),
    )
    token = ui_panel._mint_session(hass, "alice")
    actor = await settings_context(
        hass, SimpleNamespace(cookies={ui_panel._COOKIE_NAME: token})
    )
    ui_panel._sessions(hass)[token]["user_id"] = "bob"
    with pytest.raises(PermissionError, match="session"):
        await actor.credential()
