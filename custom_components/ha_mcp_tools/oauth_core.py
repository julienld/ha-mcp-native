"""Core account OAuth capabilities, separate from bearer-token admission."""

import re
from typing import Any

from .const import OAUTH_BASE

MIN_HA_OAUTH_VERSION = "2026.10.0"
HA_OAUTH_UNAVAILABLE = (
    f"Automatic Home Assistant account login requires Home Assistant {MIN_HA_OAUTH_VERSION} "
    "or newer. Existing Home Assistant bearer tokens, refresh tokens and revocation remain available."
)
_CORE_VERSION = re.compile(
    r"(20[0-9]{2})\.([1-9]|1[0-2])\.(0|[1-9][0-9]*)(?:(b|rc|\.dev)[0-9]+)?"
)


def supports_ha_oauth(installed: str) -> bool:
    """Require Core's PKCE implementation; unknown release formats fail closed."""
    if (
        not isinstance(installed, str)
        or (match := _CORE_VERSION.fullmatch(installed)) is None
    ):
        return False
    release = tuple(int(part) for part in match.group(1, 2, 3))
    minimum = tuple(int(part) for part in MIN_HA_OAUTH_VERSION.split("."))
    return release > minimum or (release == minimum and match.group(4) is None)


def authorization_server_document(base: str, installed: str) -> dict[str, Any]:
    """Describe only grants implemented by the installed Core account adapter.

    Component URLs remain stable across authentication/runtime switches. The
    scoped revoke endpoint unwraps refresh envelopes that Core cannot redeem.
    RFC 8414 section 3.2 requires omitting claims with zero array elements;
    a refresh-only server has no authorization response types to advertise.
    Keep the guarded authorize URL for clients that require it to parse even
    refresh metadata; the route explains the minimum without issuing a code.
    """
    document: dict[str, Any] = {
        "issuer": f"{base}{OAUTH_BASE}",
        "authorization_endpoint": f"{base}{OAUTH_BASE}/authorize",
        "token_endpoint": f"{base}{OAUTH_BASE}/token",
        "revocation_endpoint": f"{base}{OAUTH_BASE}/revoke",
        "revocation_endpoint_auth_methods_supported": ["none"],
        "grant_types_supported": ["refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
    }
    if supports_ha_oauth(installed):
        document.update(
            registration_endpoint=f"{base}{OAUTH_BASE}/register",
            response_types_supported=["code"],
            grant_types_supported=["authorization_code", "refresh_token"],
            code_challenge_methods_supported=["S256"],
            client_id_metadata_document_supported=True,
        )
    return document
