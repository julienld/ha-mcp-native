"""Old Core keeps bearer/refresh access without claiming PKCE code issuance."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import pytest

from ._embedded_stubs import make_request
from .test_mcp_webhook import _live_hass, _none_live_hass, mw
from .test_native_oauth import core_auth as core_auth
from .test_oauth_autoapprove import (
    DCR_KEY,
    LOOPBACK_CALLBACK,
    _CoreTokenSession,
    oauth_dcr,
    oauth_ha_auth,
)
from .test_oauth_autoapprove import (
    unified_view_client_factory as unified_view_client_factory,
)

OLD_CORES = ["2026.8.0", "2026.9.4", "unknown"]
OAUTH = "/api/ha_mcp_tools/oauth"


@pytest.mark.parametrize(
    ("installed", "supported"),
    [
        ("2026.9.9", False),
        ("2026.10.0.dev0", False),
        ("2026.10.0b1", False),
        ("2026.10.0rc1", False),
        ("2026.10.0", True),
        ("2026.10.1", True),
        ("2026.11.0.dev0", True),
        ("2027.1.0", True),
        ("2026.10", False),
        ("2026.13.0", False),
        ("unknown", False),
        ("2026.10.0garbage", False),
    ],
)
def test_account_oauth_floor_fails_closed(installed: str, supported: bool) -> None:
    from custom_components.ha_mcp_tools.oauth_core import supports_ha_oauth

    assert supports_ha_oauth(installed) is supported


@pytest.mark.parametrize("core_version", ["2026.8.0", "2026.9.4"])
async def test_real_mcp_client_refreshes_cached_grant_without_browser(
    core_version: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx2

    from ha_mcp._vendor.mcp.client.auth import OAuthClientProvider
    from ha_mcp._vendor.mcp.shared.auth import (
        OAuthClientInformationFull,
        OAuthClientMetadata,
        OAuthMetadata,
        OAuthToken,
    )

    envelope = oauth_ha_auth.wrap_refresh_token(
        DCR_KEY,
        "old-core-refresh",
        "https://original-client.example",
        "presented-client",
    )
    storage = AsyncMock()
    storage.get_tokens.return_value = OAuthToken(
        access_token="expired-fixture", refresh_token=envelope, token_type="Bearer"
    )
    storage.get_client_info.return_value = OAuthClientInformationFull(
        client_id="presented-client", token_endpoint_auth_method="none"
    )
    browser = AsyncMock()
    callback = AsyncMock()
    provider = OAuthClientProvider(
        server_url="https://ha.example/api/webhook/fixture",
        client_metadata=OAuthClientMetadata(
            redirect_uris=[LOOPBACK_CALLBACK], token_endpoint_auth_method="none"
        ),
        storage=storage,
        redirect_handler=browser,
        callback_handler=callback,
    )
    provider.context.oauth_metadata = OAuthMetadata.model_validate(
        mw._authorization_server_document("https://ha.example", core_version)
    )
    monkeypatch.setattr("time.time", lambda: 100.0)
    provider.context.token_expiry_time = 99.0
    flow = provider.async_auth_flow(httpx2.Request("POST", provider.context.server_url))
    try:
        request = await anext(flow)
        assert str(request.url) == f"https://ha.example{OAUTH}/token"
        assert parse_qs(request.content.decode())["refresh_token"] == [envelope]
        retry = await flow.asend(
            httpx2.Response(
                200,
                json={
                    "access_token": "renewed-fixture",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        )
        assert retry.headers["Authorization"] == "Bearer renewed-fixture"
        browser.assert_not_awaited()
        callback.assert_not_awaited()
    finally:
        await flow.aclose()


@pytest.mark.parametrize("core_version", OLD_CORES)
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("translated", [False, True])
async def test_new_account_login_refused_before_translation_or_dispatch(
    unified_view_client_factory: Any,
    core_auth: Any,
    monkeypatch: pytest.MonkeyPatch,
    core_version: str,
    native: bool,
    translated: bool,
) -> None:
    session = _CoreTokenSession()
    client_id = (
        oauth_dcr.mint_client_id(DCR_KEY, [LOOPBACK_CALLBACK])
        if translated
        else "http://127.0.0.1:19877"
    )
    client = await unified_view_client_factory(
        mode="ha_auth",
        core_version=core_version,
        dcr_key=DCR_KEY,
        session=session,
        prepare_hass=core_auth.prepare if native else lambda *_args: None,
    )
    translate = AsyncMock(return_value="http://127.0.0.1:19877")
    monkeypatch.setattr(oauth_ha_auth, "resolve_forward_client_id", translate)
    params = {
        "client_id": client_id,
        "redirect_uri": LOOPBACK_CALLBACK,
        "response_type": "code",
        "code_challenge": "a" * 43,
        "code_challenge_method": "S256",
    }
    authorize = await client.get(
        f"{OAUTH}/authorize", params=params, allow_redirects=False
    )
    token = await client.post(
        f"{OAUTH}/token",
        data={**params, "grant_type": "authorization_code", "code": "existing-code"},
        allow_redirects=False,
    )
    for response, error in (
        (authorize, "unsupported_response_type"),
        (token, "unsupported_grant_type"),
    ):
        assert response.status == 400
        assert "Location" not in response.headers
        body = await response.json()
        assert body["error"] == error
        assert "2026.10.0" in body["error_description"]
    translate.assert_not_awaited()
    assert core_auth.calls == []
    assert session.calls == []


@pytest.mark.parametrize("core_version", OLD_CORES)
@pytest.mark.parametrize("native", [False, True])
async def test_existing_signed_refresh_and_revocation_survive_old_core(
    unified_view_client_factory: Any,
    core_auth: Any,
    monkeypatch: pytest.MonkeyPatch,
    core_version: str,
    native: bool,
) -> None:
    session = _CoreTokenSession()
    monkeypatch.setattr(
        oauth_ha_auth, "core_token_base_url", lambda _hass: "https://core.example"
    )
    client = await unified_view_client_factory(
        mode="ha_auth",
        core_version=core_version,
        dcr_key=DCR_KEY,
        session=session,
        prepare_hass=core_auth.prepare if native else lambda *_args: None,
    )
    envelope = oauth_ha_auth.wrap_refresh_token(
        DCR_KEY,
        "old-core-refresh",
        "https://original-client.example",
        "presented-client",
    )
    refresh = await client.post(
        f"{OAUTH}/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": envelope,
            "client_id": "presented-client",
        },
        allow_redirects=False,
    )
    assert refresh.status == 200
    form = core_auth.calls[0][1] if native else session.calls[0]["data"]
    assert form["refresh_token"] == "old-core-refresh"
    assert form["client_id"] == "https://original-client.example"
    for surface in ("revoke", "token"):
        revoke = await client.post(
            f"{OAUTH}/{surface}",
            data={"action": "revoke", "token": envelope},
            allow_redirects=False,
        )
        assert revoke.status == 200
    if native:
        assert core_auth.calls[1:] == [("revoke", "fixture-owner")] * 2
    else:
        assert [call["data"]["token"] for call in session.calls[1:]] == [
            "old-core-refresh"
        ] * 2


@pytest.mark.parametrize("core_version", [*OLD_CORES, "2026.10.0", "2026.10.3"])
@pytest.mark.parametrize("mode", ["ha_auth", "none", "legacy"])
async def test_discovery_matches_the_active_mode_and_core(
    monkeypatch: pytest.MonkeyPatch, core_version: str, mode: str
) -> None:
    monkeypatch.setattr(mw, "HA_VERSION", core_version)
    hass = _none_live_hass() if mode == "none" else _live_hass(mode)
    response = await mw._AuthorizationServerMetadataView(hass).get(
        make_request(headers={"Host": "ha.example"})
    )
    assert response.status == 200
    document = response.json_body
    available = mode != "ha_auth" or core_version in ("2026.10.0", "2026.10.3")
    assert document["token_endpoint"] == f"https://ha.example{OAUTH}/token"
    assert document["authorization_endpoint"] == f"https://ha.example{OAUTH}/authorize"
    assert all(value != [] for value in document.values())
    if available:
        assert (
            document["authorization_endpoint"] == f"https://ha.example{OAUTH}/authorize"
        )
        assert document["response_types_supported"] == ["code"]
        assert document["code_challenge_methods_supported"] == ["S256"]
        assert "authorization_code" in document["grant_types_supported"]
    else:
        assert document["grant_types_supported"] == ["refresh_token"]
        assert document["revocation_endpoint"] == f"https://ha.example{OAUTH}/revoke"
        assert document["token_endpoint_auth_methods_supported"] == ["none"]
        for field in (
            "response_types_supported",
            "code_challenge_methods_supported",
            "registration_endpoint",
            "client_id_metadata_document_supported",
        ):
            assert field not in document


def test_ha_auth_document_advertises_the_scoped_revocation_endpoint():
    """#2248: the client holds an envelope, so revocation must come to us.

    None mode issues no refresh token at all, so its document keeps quiet —
    advertising a route that 404s there would be worse than silence.
    """
    from custom_components.ha_mcp_tools import mcp_webhook

    base = "https://ha.example"
    ha_auth_doc = mcp_webhook._authorization_server_document(base, "2026.10.3")
    assert ha_auth_doc["revocation_endpoint"] == f"{base}{OAUTH}/revoke"
    assert ha_auth_doc["revocation_endpoint_auth_methods_supported"] == ["none"]

    none_doc = mcp_webhook._none_mode_authorization_server_document(base)
    assert "revocation_endpoint" not in none_doc
    legacy_doc = mcp_webhook._legacy_authorization_server_document(base)
    assert "revocation_endpoint" not in legacy_doc


def test_authorization_server_document_shape():
    doc = mw._authorization_server_document("https://x.nabu.casa", "2026.10.3")
    assert doc["issuer"] == f"https://x.nabu.casa{OAUTH}"
    assert doc["authorization_endpoint"] == f"https://x.nabu.casa{OAUTH}/authorize"
    assert doc["token_endpoint"] == f"https://x.nabu.casa{OAUTH}/token"
    assert doc["response_types_supported"] == ["code"]
    assert doc["code_challenge_methods_supported"] == ["S256"]
    assert doc["token_endpoint_auth_methods_supported"] == ["none"]
    assert doc["client_id_metadata_document_supported"] is True
    assert doc["registration_endpoint"] == f"https://x.nabu.casa{OAUTH}/register"
