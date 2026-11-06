"""Native catalog aliases preserve endpoint mode and read-only admission."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from .test_mcp_webhook import (
    DATA_WEBHOOK,
    DOMAIN,
    WEBHOOK_ID,
    FakeSession,
    _make_hass,
    _store_cfg,
    make_request,
    mw,
)


@pytest.mark.parametrize(
    "suffix,mode,readonly",
    [("/catalog/full", "full", False), ("/readonly/catalog/search", "search", True)],
)
async def test_catalog_alias_preserves_native_mode_and_readonly(suffix, mode, readonly):
    hass = _make_hass()
    _store_cfg(hass, session=FakeSession())
    native = SimpleNamespace(
        async_handle_http=AsyncMock(return_value=SimpleNamespace(status=200))
    )
    hass.data[DOMAIN][DATA_WEBHOOK]["native_manager"] = native
    await mw._async_handle_webhook(hass, WEBHOOK_ID, make_request(), suffix=suffix)
    assert native.async_handle_http.await_args.kwargs == {
        "read_only": readonly,
        "catalog_mode": mode,
    }
