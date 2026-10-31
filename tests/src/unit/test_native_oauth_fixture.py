"""Older Core qualification restores a real bound grant, never a fake code flow."""

import runpy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from .test_oauth_autoapprove import DCR_KEY, LOOPBACK_CALLBACK, oauth_dcr, oauth_ha_auth


@pytest.fixture
def existing_grant() -> Any:
    return runpy.run_path(
        str(
            Path(__file__).parents[2]
            / "native/fixtures/native_fixture/oauth_support.py"
        )
    )["issue_existing_oauth_grant"]


async def test_restored_grant_keeps_real_core_owner_and_original_client_binding(
    existing_grant: Any,
) -> None:
    credential = SimpleNamespace(auth_provider_type="homeassistant")
    user = SimpleNamespace(credentials=[credential])
    refresh = SimpleNamespace(token="real-core-refresh")
    create = AsyncMock(return_value=refresh)
    access = Mock(return_value="real-core-access")
    hass = SimpleNamespace(
        data={"ha_mcp_tools": {"webhook": {"dcr_signing_key": DCR_KEY}}},
        auth=SimpleNamespace(
            async_create_refresh_token=create, async_create_access_token=access
        ),
    )
    client = oauth_dcr.mint_client_id(DCR_KEY, [LOOPBACK_CALLBACK])
    result = await existing_grant(
        hass, user, {"client_id": client, "redirect_uri": LOOPBACK_CALLBACK}
    )
    forward = await oauth_ha_auth.resolve_forward_client_id(
        None, DCR_KEY, client, LOOPBACK_CALLBACK
    )
    create.assert_awaited_once_with(user, forward, credential=credential)
    access.assert_called_once_with(refresh)
    assert result["access_token"] == "real-core-access"
    restored = oauth_ha_auth.unwrap_refresh_token(
        DCR_KEY, result["refresh_token"], client
    )
    assert restored == ("real-core-refresh", forward)
    assert (
        oauth_ha_auth.unwrap_refresh_token(
            DCR_KEY, result["refresh_token"], "unrelated-client"
        )
        is oauth_ha_auth.EnvelopeState.INVALID
    )


async def test_unregistered_callback_is_refused_before_creating_a_core_grant(
    existing_grant: Any,
) -> None:
    create = AsyncMock()
    hass = SimpleNamespace(
        data={"ha_mcp_tools": {"webhook": {"dcr_signing_key": DCR_KEY}}},
        auth=SimpleNamespace(async_create_refresh_token=create),
    )
    with pytest.raises(ValueError, match="registered"):
        await existing_grant(
            hass,
            SimpleNamespace(credentials=[]),
            {
                "client_id": oauth_dcr.mint_client_id(DCR_KEY, [LOOPBACK_CALLBACK]),
                "redirect_uri": "http://127.0.0.1:39999/unregistered",
            },
        )
    create.assert_not_awaited()
