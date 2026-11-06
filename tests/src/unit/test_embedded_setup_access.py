"""Access-switch startup and teardown across native and legacy runtimes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from .test_embedded_setup import (
    DATA_MANAGER,
    DOMAIN,
    _make_entry,
    _make_hass,
    esetup,
    install_spies,
    make_fake_manager,
)


@pytest.mark.parametrize("failing_step", [None, "llm", "webhook", "manager"])
async def test_access_off_stops_native_work_even_when_ingress_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch, failing_step: str | None
) -> None:
    """Native managers must drain on Access off just as on entry unload."""
    install_spies(monkeypatch)
    hass = _make_hass()
    manager = SimpleNamespace(async_stop=AsyncMock())
    hass.data = {DOMAIN: {DATA_MANAGER: manager}}
    steps = {
        "llm": esetup.async_unregister_llm_api,
        "webhook": esetup.async_unregister_webhook,
        "manager": manager.async_stop,
    }
    dismiss = MagicMock()
    monkeypatch.setattr(esetup.persistent_notification, "async_dismiss", dismiss)
    if failing_step:
        steps[failing_step].side_effect = RuntimeError("cleanup failed")
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await esetup.async_teardown_server(hass)
    else:
        await esetup.async_teardown_server(hass)

    esetup.async_unregister_llm_api.assert_called_once_with(hass)
    esetup.async_unregister_webhook.assert_awaited_once_with(hass)
    manager.async_stop.assert_awaited_once()
    if failing_step == "manager":
        # Keep ownership of incomplete cleanup so the next stop can drain it
        # before an Access-on request creates another runtime.
        assert hass.data[DOMAIN][DATA_MANAGER] is manager
        manager.async_stop.side_effect = None
        await esetup.async_teardown_server(hass)
        assert manager.async_stop.await_count == 2
    assert DATA_MANAGER not in hass.data[DOMAIN]
    dismiss.assert_called_with(hass, esetup._NOTIFICATION_ID)
    cleared = {call.args[2] for call in esetup.ir.async_delete_issue.call_args_list}
    assert set(esetup._ISSUE_IDS) <= cleared


async def test_start_from_the_access_switch_reaches_the_notification_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_spies(monkeypatch)
    make_fake_manager(monkeypatch)
    hass = _make_hass()
    entry = _make_entry()

    await esetup.async_bring_up_server(hass, entry, announce=False)

    assert esetup._surface_connect_urls.call_args.kwargs["announce"] is False
