"""Issue real Core grants for an already authenticated disposable fixture user.

The fixture replaces only the human login step. Core owns the stored code,
PKCE challenge, expiry and single-use redemption exercised over the real HTTP
OAuth endpoints. On older Core, restore an existing bound refresh grant without
exercising the intentionally unavailable new authorization-code flow. Values
returned here must never be written to evidence logs.
"""

from typing import Any


async def seed_oauth(
    hass: Any, user: Any, operation: str, body: dict[str, Any]
) -> dict[str, str]:
    if operation == "oauth_code":
        return issue_oauth_code(hass, user, body)
    if operation == "oauth_existing_client":
        return issue_existing_oauth_client(hass, body)
    if operation == "oauth_existing_grant":
        return await issue_existing_oauth_grant(hass, user, body)
    raise ValueError("Unknown fixture OAuth operation")


def _credential(user: Any) -> Any:
    return next(
        credential
        for credential in user.credentials
        if credential.auth_provider_type == "homeassistant"
    )


def issue_oauth_code(hass: Any, user: Any, body: dict[str, Any]) -> dict[str, str]:
    from homeassistant.components.auth import DATA_STORE

    code = hass.data[DATA_STORE](
        str(body["client_id"]),
        _credential(user),
        str(body["code_challenge"]),
        "S256",
    )
    return {"code": code}


def issue_existing_oauth_client(hass: Any, body: dict[str, Any]) -> dict[str, str]:
    """Restore an old registration without calling the gated DCR endpoint."""
    from custom_components.ha_mcp_tools.const import DATA_WEBHOOK, DOMAIN
    from custom_components.ha_mcp_tools.oauth_dcr import (
        CFG_DCR_SIGNING_KEY,
        mint_client_id,
    )

    key = hass.data[DOMAIN][DATA_WEBHOOK][CFG_DCR_SIGNING_KEY]
    return {"client_id": mint_client_id(key, [str(body["redirect_uri"])])}


async def issue_existing_oauth_grant(
    hass: Any, user: Any, body: dict[str, Any]
) -> dict[str, str]:
    from custom_components.ha_mcp_tools.const import DATA_WEBHOOK, DOMAIN
    from custom_components.ha_mcp_tools.oauth_dcr import (
        CFG_DCR_SIGNING_KEY,
        client_redirect_uris,
    )
    from custom_components.ha_mcp_tools.oauth_ha_auth import (
        resolve_forward_client_id,
        wrap_refresh_token,
    )

    key = hass.data[DOMAIN][DATA_WEBHOOK][CFG_DCR_SIGNING_KEY]
    client_id, redirect_uri = str(body["client_id"]), str(body["redirect_uri"])
    if redirect_uri not in (client_redirect_uris(key, client_id) or []):
        raise ValueError("Fixture grant requires a registered client/callback pair")
    forward = await resolve_forward_client_id(None, key, client_id, redirect_uri)
    refresh = await hass.auth.async_create_refresh_token(
        user, forward, credential=_credential(user)
    )
    return {
        "access_token": hass.auth.async_create_access_token(refresh),
        "refresh_token": wrap_refresh_token(key, refresh.token, forward, client_id),
    }
