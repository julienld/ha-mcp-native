"""Real Core translated OAuth grants, with the human login step fixture-owned."""

import base64
import hashlib
import secrets
from typing import Any
from urllib.parse import parse_qs, urlsplit

_BASE = "http://127.0.0.1:8123"
_OAUTH = _BASE + "/api/ha_mcp_tools/oauth"
_CALLBACK = "http://127.0.0.1:31999/native-callback"


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    return verifier, challenge


async def _register(checks: Any, callback: str = _CALLBACK) -> str:
    response = await checks.client.post(
        _OAUTH + "/register",
        json={
            "client_name": "Disposable native OAuth check",
            "redirect_uris": [callback],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert response.status_code == 201, "DCR registration failed"
    return response.json()["client_id"]


async def _code(checks: Any, client_id: str) -> tuple[str, str]:
    verifier, challenge = _pkce()
    response = await checks.client.get(
        _OAUTH + "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": _CALLBACK,
            "response_type": "code",
            "state": "native-oauth-fixture",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302, "Translated authorize failed"
    target = urlsplit(response.headers["location"])
    assert target.path == "/auth/authorize", "Authorize bypassed Core"
    query = parse_qs(target.query)
    assert query["redirect_uri"] == [_CALLBACK], "Redirect binding changed"
    assert query["code_challenge"] == [challenge], "PKCE challenge changed"
    seeded = await checks.control(
        "oauth_code",
        {"client_id": query["client_id"][0], "code_challenge": challenge},
    )
    return seeded["code"], verifier


async def _exchange(checks: Any, client_id: str, code: str, verifier: str) -> Any:
    return await checks.client.post(
        _OAUTH + "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": _CALLBACK,
            "code": code,
            "code_verifier": verifier,
        },
        follow_redirects=False,
    )


async def _refresh(checks: Any, client_id: str, token: str) -> Any:
    return await checks.client.post(
        _OAUTH + "/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": token,
        },
        follow_redirects=False,
    )


async def exercise_oauth(checks: Any) -> None:
    client_id = await _register(checks)
    code, verifier = await _code(checks, client_id)
    response = await _exchange(checks, client_id, code, verifier)
    assert response.status_code == 200, "Native Core code exchange failed"
    grant = response.json()
    envelope = grant["refresh_token"]
    assert envelope.startswith("hamcp-rt-"), "Refresh token lost the translated binding"
    accepted = await checks.client.get(
        _BASE + "/api/", headers={"Authorization": "Bearer " + grant["access_token"]}
    )
    assert accepted.status_code == 200, "Issued token is not accepted by Core"
    replay = await _exchange(checks, client_id, code, verifier)
    assert replay.status_code == 400, "Core authorization code was replayable"
    renewed = await _refresh(checks, client_id, envelope)
    assert renewed.status_code == 200, "Translated refresh failed"
    checks.passed(
        "native_OAuth_DCR_Core_PKCE_exchange_single_use_and_refresh_without_loopback"
    )

    other_client = await _register(checks, _CALLBACK + "-other")
    denied = await _refresh(checks, other_client, envelope)
    assert denied.status_code == 400, "Another DCR client redeemed the refresh envelope"
    code, verifier = await _code(checks, client_id)
    wrong, _challenge = _pkce()
    denied = await _exchange(checks, client_id, code, wrong)
    assert denied.status_code == 400, "Core accepted an invalid PKCE verifier"
    replay = await _exchange(checks, client_id, code, verifier)
    assert replay.status_code == 400, "Core failed to consume a rejected code"
    checks.passed("native_OAuth_Core_PKCE_failure_and_refresh_client_binding_preserved")

    revoked = await checks.client.post(_OAUTH + "/revoke", data={"token": envelope})
    assert revoked.status_code == 200, "Scoped Core revocation failed"
    refused = await _refresh(checks, client_id, envelope)
    assert refused.status_code == 400, "Revoked refresh token remains usable"
    again = await checks.client.post(_OAUTH + "/revoke", data={"token": envelope})
    assert again.status_code == 200, "Revocation lost RFC 7009 idempotency"

    code, verifier = await _code(checks, client_id)
    response = await _exchange(checks, client_id, code, verifier)
    assert response.status_code == 200, "Second native grant failed"
    envelope = response.json()["refresh_token"]
    revoked = await checks.client.post(
        _OAUTH + "/token", data={"action": "revoke", "token": envelope}
    )
    assert revoked.status_code == 200, "Legacy action=revoke did not reach Core"
    refused = await _refresh(checks, client_id, envelope)
    assert refused.status_code == 400, "Legacy revocation did not invalidate Core token"
    checks.passed("native_OAuth_Core_scoped_and_legacy_revocation_preserved")
