"""Issue real Core codes for an already authenticated disposable fixture user.

The fixture replaces only the human login step. Core owns the stored code,
PKCE challenge, expiry and single-use redemption exercised over the real HTTP
OAuth endpoints. Values returned here must never be written to evidence logs.
"""

from typing import Any


def issue_oauth_code(hass: Any, user: Any, body: dict[str, Any]) -> dict[str, str]:
    from homeassistant.components.auth import DATA_STORE

    credential = next(
        credential
        for credential in user.credentials
        if credential.auth_provider_type == "homeassistant"
    )
    code = hass.data[DATA_STORE](
        str(body["client_id"]),
        credential,
        str(body["code_challenge"]),
        "S256",
    )
    return {"code": code}
