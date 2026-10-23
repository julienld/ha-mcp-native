"""Persist bridge migration before installing the entry's options listener."""

from unittest.mock import MagicMock

import pytest

from .test_embedded_entry import (
    _drain_background_tasks,
    _make_entry,
    _make_hass,
    eentry,
)
from .test_embedded_entry import (
    fake_collaborators as fake_collaborators,
)


@pytest.mark.parametrize(
    "saved,expected",
    [
        ({}, {"enable_llm_api": False, "enable_native_llm_import": False}),
        (
            {"enable_llm_api": True, "native_import_apis": ["assist"]},
            {
                "enable_llm_api": True,
                "enable_native_llm_import": True,
                "native_import_apis": ["assist"],
            },
        ),
        (
            {
                "enable_llm_api": True,
                "native_import_apis": ["assist"],
                "enable_native_llm_import": False,
            },
            {
                "enable_llm_api": True,
                "native_import_apis": ["assist"],
                "enable_native_llm_import": False,
            },
        ),
    ],
)
@pytest.mark.usefixtures("fake_collaborators")
async def test_entry_migrates_without_reloading_or_replacing_identity(
    saved: dict, expected: dict
) -> None:
    hass, entry = _make_hass(), _make_entry()
    entry.options = dict(saved)
    original_data = dict(entry.data)

    def update(_entry, *, options=None, **kwargs):
        entry.add_update_listener.assert_not_called()
        assert not kwargs
        entry.options = options

    hass.config_entries.async_update_entry = MagicMock(side_effect=update)
    await eentry.async_setup_server_entry(hass, entry)
    await _drain_background_tasks(hass, entry)
    assert entry.options == expected
    assert entry.entry_id == "entry-1" and entry.data == original_data
    assert hass.data[eentry.DOMAIN][eentry.DATA_LAST_OPTIONS] == expected
    hass.config_entries.async_reload.assert_not_called()
    assert hass.config_entries.async_update_entry.call_count == (saved != expected)
