"""Setup and options share independent default-off Core LLM bridge controls."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from .test_config_flow import _make_flow, _make_options_flow, cf, const


async def test_setup_bridge_defaults_and_saved_choices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cf, "HA_VERSION", "2026.10.2")
    flow = _make_flow()
    flow.hass.auth.async_get_users = AsyncMock(
        return_value=[
            SimpleNamespace(
                id="owner",
                name="Owner",
                is_active=True,
                is_admin=True,
                system_generated=False,
            )
        ]
    )
    form = await flow.async_step_server()
    markers = {m.schema: m for m in form["data_schema"].schema}
    for key in ("enable_llm_api", "enable_native_llm_import"):
        assert markers[key].default() is False
    empty = await flow.async_step_server({const.DATA_NATIVE_OWNER_ID: "owner"})
    assert empty["options"]["enable_llm_api"] is False
    assert empty["options"]["enable_native_llm_import"] is False
    assert empty["options"]["enable_webhook"] is False
    # New entries carry the current schema, so a later setup must not apply
    # the historical export-on default to a newly created default-off entry.
    import custom_components.ha_mcp_tools as component

    entry = SimpleNamespace(
        version=flow.VERSION,
        minor_version=flow.MINOR_VERSION,
        data=empty["data"],
        options=empty["options"],
    )
    assert (entry.version, entry.minor_version) == (1, 3)
    hass = MagicMock()
    assert await component.async_migrate_entry(hass, entry)
    hass.config_entries.async_update_entry.assert_not_called()
    enabled = await flow.async_step_server(
        {
            const.DATA_NATIVE_OWNER_ID: "owner",
            "enable_llm_api": True,
            "enable_native_llm_import": True,
            "native_import_apis": ["assist"],
        }
    )
    assert enabled["options"]["enable_llm_api"] is True
    assert enabled["options"]["enable_native_llm_import"] is True
    assert enabled["options"]["native_import_apis"] == ["assist"]
    assert const.DATA_NATIVE_OWNER_ID not in enabled["options"]


@pytest.mark.parametrize(
    "saved,export,imports",
    [
        ({}, False, False),
        ({"enable_llm_api": True}, True, False),
        ({"native_import_apis": ["assist"]}, False, True),
        (
            {"native_import_apis": ["assist"], "enable_native_llm_import": False},
            False,
            False,
        ),
    ],
)
async def test_options_prefill_migration_and_partial_save(
    saved: dict, export: bool, imports: bool
) -> None:
    flow = _make_options_flow(options=saved)
    form = await flow.async_step_init()
    markers = {m.schema: m for m in form["data_schema"].schema}
    assert markers["enable_llm_api"].default() is export
    assert markers["enable_native_llm_import"].default() is imports
    saved_form = await flow.async_step_init({"enable_webhook": False})
    assert saved_form["data"]["enable_llm_api"] is export
    assert saved_form["data"]["enable_native_llm_import"] is imports
    if "native_import_apis" in saved:
        assert saved_form["data"]["native_import_apis"] == saved["native_import_apis"]


async def test_disabling_import_keeps_ids_for_later_reactivation() -> None:
    flow = _make_options_flow(
        options={"enable_llm_api": True, "native_import_apis": ["assist"]}
    )
    result = await flow.async_step_init({"enable_native_llm_import": False})
    assert result["data"]["enable_llm_api"] is True
    assert result["data"]["enable_native_llm_import"] is False
    assert result["data"]["native_import_apis"] == ["assist"]


@pytest.mark.parametrize(
    "saved,enabled", [({}, True), ({"enable_llm_api": False}, False)]
)
@pytest.mark.parametrize("minor", [1, 2])
async def test_configure_before_first_setup_preserves_old_effective_export(
    saved: dict, enabled: bool, minor: int
) -> None:
    """A disabled old entry can open Configure before Core migrates its setup."""
    override = {"pip_spec": "ha-mcp-dev==9.0.0.dev3136"}
    flow = _make_options_flow(options={**override, **saved})
    flow.config_entry.version = 1
    flow.config_entry.minor_version = minor
    form = await flow.async_step_init()
    markers = {marker.schema: marker for marker in form["data_schema"].schema}
    assert markers["enable_llm_api"].default() is enabled
    result = await flow.async_step_init({"enable_webhook": False, **override})
    assert result["data"]["enable_llm_api"] is enabled
    assert result["data"]["enable_native_llm_import"] is False
    assert result["data"]["pip_spec"] == override["pip_spec"]
