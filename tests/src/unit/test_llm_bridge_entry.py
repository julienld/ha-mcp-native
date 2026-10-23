"""Keep existing Assist choices through HA's versioned entry migration."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import fake_llm_apis, install

install()

import custom_components.ha_mcp_tools as component  # noqa: E402
from custom_components.ha_mcp_tools import llm_api, native_llm  # noqa: E402

from ._llm_api_helpers import make_hass  # noqa: E402
from .test_embedded_entry import (  # noqa: E402
    _drain_background_tasks,
    _make_entry,
    _make_hass,
    eentry,
)
from .test_embedded_entry import (  # noqa: E402
    fake_collaborators as fake_collaborators,
)


@pytest.mark.parametrize(
    "saved,expected",
    [
        ({}, {"enable_llm_api": True, "enable_native_llm_import": False}),
        (
            {"enable_llm_api": False, "native_import_apis": ["assist"]},
            {
                "enable_llm_api": False,
                "enable_native_llm_import": True,
                "native_import_apis": ["assist"],
            },
        ),
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
@pytest.mark.parametrize("minor", [1, 2])
async def test_entry_migrates_without_reloading_or_replacing_identity(
    saved: dict, expected: dict, minor: int
) -> None:
    hass, entry = _make_hass(), _make_entry()
    entry.version, entry.minor_version = 1, minor
    entry.data["entry_type"] = "server"
    # Upstream 1.2 already cleared old overrides; a saved one is a later choice.
    override = {"pip_spec": "ha-mcp-dev==9.0.0.dev3136"}
    entry.options = {"server_port": 9584, **override, **saved}
    expected = {"server_port": 9584, **(override if minor == 2 else {}), **expected}
    original_data = dict(entry.data)

    def update(_entry: object, **changes: object) -> None:
        entry.add_update_listener.assert_not_called()
        for key, value in changes.items():
            setattr(entry, key, value)

    hass.config_entries.async_update_entry = MagicMock(side_effect=update)
    assert await component.async_migrate_entry(hass, entry)
    await eentry.async_setup_server_entry(hass, entry)
    await _drain_background_tasks(hass, entry)
    assert entry.options == expected
    assert entry.entry_id == "entry-1" and entry.data == original_data
    assert hass.data[eentry.DOMAIN][eentry.DATA_LAST_OPTIONS] == expected
    hass.config_entries.async_reload.assert_not_called()
    assert hass.config_entries.async_update_entry.call_count == 1
    assert (entry.version, entry.minor_version) == (1, 3)

    await eentry.async_unload_server_entry(hass, entry)
    assert await component.async_migrate_entry(hass, entry)
    await eentry.async_setup_server_entry(hass, entry)
    await _drain_background_tasks(hass, entry)
    assert entry.options == expected
    assert hass.config_entries.async_update_entry.call_count == 1
    hass.config_entries.async_reload.assert_not_called()


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("minor", [1, 2])
async def test_old_implicit_export_keeps_assist_api_selection_on_upgrade(
    monkeypatch: pytest.MonkeyPatch, native: bool, minor: int
) -> None:
    hass = make_hass()
    saved = {"llm_api_exposure": "both", "native_import_apis": ["assist"]}
    if native:
        saved["runtime_mode"] = "native"
    entry = SimpleNamespace(
        version=1,
        minor_version=minor,
        options=saved,
        data={"entry_type": "server"},
        entry_id="historic",
        title="Historic",
    )

    def update(_entry: object, **changes: object) -> None:
        for key, value in changes.items():
            setattr(entry, key, value)

    hass.config_entries.async_update_entry = MagicMock(side_effect=update)
    monkeypatch.setattr(llm_api, "async_probe_mcp_sdk", AsyncMock(return_value=True))
    monkeypatch.setattr(native_llm, "_schema_converter", lambda: None)
    assert await component.async_migrate_entry(hass, entry)
    for _ in range(2):
        if native:
            await native_llm.async_register_native_llm(
                hass, entry, SimpleNamespace(entry=entry)
            )
        else:
            await llm_api.async_register_llm_api(
                hass, entry, port=9584, secret_path="/unchanged"
            )
        assert set(fake_llm_apis(hass)) == {
            "ha_mcp_tools-historic",
            "ha_mcp_tools-historic-toolsearch",
        }
    assert entry.options == {
        **saved,
        "enable_llm_api": True,
        "enable_native_llm_import": True,
    }


@pytest.mark.parametrize("data", [{}, {"entry_type": "tools"}])
async def test_tools_entry_migration_never_introduces_bridge_options(
    data: dict,
) -> None:
    hass = make_hass()
    entry = SimpleNamespace(
        version=1, minor_version=1, data=data, options={"allowed_paths": ["private"]}
    )
    assert await component.async_migrate_entry(hass, entry)
    hass.config_entries.async_update_entry.assert_called_once_with(
        entry, minor_version=3
    )
    assert entry.options == {"allowed_paths": ["private"]}
    assert not fake_llm_apis(hass)


@pytest.mark.parametrize(
    "version,minor,accepted", [(1, 3, True), (1, 4, True), (2, 1, False)]
)
async def test_future_entry_schema_is_never_rewritten(
    version: int, minor: int, accepted: bool
) -> None:
    hass = make_hass()
    entry = SimpleNamespace(version=version, minor_version=minor)
    assert await component.async_migrate_entry(hass, entry) is accepted
    hass.config_entries.async_update_entry.assert_not_called()
