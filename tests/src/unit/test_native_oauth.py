"""Native OAuth translation must reach Core without a relay HTTP session."""

from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from .test_oauth_autoapprove import (
    DCR_KEY,
    LOOPBACK_CALLBACK,
    aa,
    oauth_dcr,
    oauth_ha_auth,
)
from .test_oauth_autoapprove import (
    unified_view_client_factory as unified_view_client_factory,
)


@pytest.fixture
def core_auth(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    import sys

    calls = []
    state = SimpleNamespace()

    class TokenView:
        name = "api:auth:token"
        url = "/auth/token"

        async def _async_handle_auth_code(
            self, hass: Any, data: Any, request: Any
        ) -> Any:
            calls.append(("code", dict(data), request))
            return aa.web.json_response(
                {"access_token": "fixture-access", "refresh_token": "fixture-refresh"}
            )

        async def _async_handle_refresh_token(
            self, hass: Any, data: Any, request: Any
        ) -> Any:
            calls.append(("refresh", dict(data), request))
            return aa.web.json_response({"access_token": "fixture-renewed"})

    owner = TokenView()
    module = ModuleType("homeassistant.components.auth")
    module.TokenView = TokenView
    monkeypatch.setitem(sys.modules, module.__name__, module)
    ban = ModuleType("homeassistant.components.http.ban")
    ban.log_invalid_auth = lambda callback: callback
    monkeypatch.setitem(sys.modules, ban.__name__, ban)

    def prepare(hass: Any, app: Any) -> None:
        from custom_components.ha_mcp_tools import native_oauth

        monkeypatch.setattr(native_oauth, "web", aa.web)
        cfg = hass.data["ha_mcp_tools"]["webhook"]
        cfg["auth_mode"] = "ha_auth"
        cfg["native_manager"] = SimpleNamespace(is_running=True)
        state.hass, state.cfg = hass, cfg
        hass.http.app = app
        hass.auth = SimpleNamespace(
            async_get_refresh_token_by_token=lambda _token: "fixture-owner",
            async_remove_refresh_token=lambda token: calls.append(("revoke", token)),
        )
        view = owner

        async def core_route(request: Any) -> Any:
            # Same owner closure as Core's request_handler_factory; this
            # transport handler itself must never be called by native OAuth.
            assert view is owner
            raise AssertionError("Native OAuth dispatched a Core HTTP handler")

        app.router.add_post("/auth/token", core_route)

    return SimpleNamespace(owner=owner, calls=calls, prepare=prepare, state=state)


async def test_native_translated_code_and_refresh_work_without_relay_session(
    unified_view_client_factory: Any, core_auth: Any
) -> None:
    client_id = oauth_dcr.mint_client_id(DCR_KEY, [LOOPBACK_CALLBACK])
    client = await unified_view_client_factory(
        mode="ha_auth", dcr_key=DCR_KEY, prepare_hass=core_auth.prepare
    )
    response = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": "fixture-code",
            "client_id": client_id,
            "redirect_uri": LOOPBACK_CALLBACK,
            "code_verifier": "fixture-verifier",
        },
    )
    assert response.status == 200
    grant = await response.json()
    assert grant["refresh_token"].startswith("hamcp-rt-")
    assert core_auth.calls[0][1]["client_id"] == "http://127.0.0.1:19877"
    assert core_auth.calls[0][1]["code_verifier"] == "fixture-verifier"
    assert core_auth.calls[0][2].path == "/api/ha_mcp_tools/oauth/token"
    response = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": grant["refresh_token"],
            "client_id": client_id,
        },
    )
    assert response.status == 200
    assert core_auth.calls[1][1]["refresh_token"] == "fixture-refresh"
    assert core_auth.calls[1][1]["client_id"] == "http://127.0.0.1:19877"


@pytest.mark.parametrize("surface", ["token", "revoke"])
async def test_native_revocation_removes_the_unwrapped_core_token(
    unified_view_client_factory: Any, core_auth: Any, surface: str
) -> None:
    envelope = oauth_ha_auth.wrap_refresh_token(
        DCR_KEY, "fixture-refresh", "https://fixture.example", "fixture-client"
    )
    client = await unified_view_client_factory(
        mode="ha_auth", dcr_key=DCR_KEY, prepare_hass=core_auth.prepare
    )
    response = await client.post(
        f"/api/ha_mcp_tools/oauth/{surface}",
        data={"action": "revoke", "token": envelope},
    )
    assert response.status == 200
    assert core_auth.calls == [("revoke", "fixture-owner")]


async def test_native_entry_removed_during_translation_does_not_consume_code(
    unified_view_client_factory: Any, core_auth: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client_id = oauth_dcr.mint_client_id(DCR_KEY, [LOOPBACK_CALLBACK])
    client = await unified_view_client_factory(
        mode="ha_auth", dcr_key=DCR_KEY, prepare_hass=core_auth.prepare
    )
    original = oauth_ha_auth.resolve_forward_client_id

    async def translate(*args: Any) -> str:
        result = await original(*args)
        core_auth.state.hass.data["ha_mcp_tools"].pop("webhook")
        return result

    monkeypatch.setattr(oauth_ha_auth, "resolve_forward_client_id", translate)
    response = await client.post(
        "/api/ha_mcp_tools/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": "fixture-code",
            "client_id": client_id,
            "redirect_uri": LOOPBACK_CALLBACK,
        },
    )
    assert response.status == 503
    assert not core_auth.calls
