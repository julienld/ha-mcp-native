"""Older supported Core releases keep their Legacy server and Assist ABI."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from .test_config_flow import _make_flow, cf, const
from .test_embedded_server import _manager, es


@pytest.mark.parametrize("version", ["2026.8.0", "2026.9.0"])
async def test_supported_old_core_reaches_legacy_package_install(
    tmp_path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    class PackageReached(Exception):
        pass

    manager, _, _ = _manager(tmp_path)
    monkeypatch.setattr(es, "HA_VERSION", version)
    monkeypatch.setattr(
        es, "_async_paired_server_requirement", AsyncMock(return_value="ha-mcp==9.1.0")
    )
    install = AsyncMock(side_effect=PackageReached)
    monkeypatch.setattr(manager, "_async_ensure_package", install)
    with pytest.raises(PackageReached):
        await manager.async_start()
    install.assert_awaited_once()


@pytest.mark.parametrize("version", ["2026.8.0", "2026.9.0"])
async def test_old_core_can_create_legacy_server_with_existing_setup_contract(
    monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    monkeypatch.setattr(cf, "HA_VERSION", version)
    credentials = AsyncMock(return_value=("administrator-token", None))
    monkeypatch.setattr(
        cf.server_credentials, "async_credential_from_input", credentials
    )
    flow = _make_flow()
    result = await flow.async_step_server(
        {
            cf.SETUP_ADMIN_TOKEN: "administrator-token",
            cf.SETUP_REMOTE_ACCESS: const.WEBHOOK_AUTH_NONE,
            const.OPT_BIND_HOST: const.BIND_HOST_ALL,
        }
    )
    assert result["type"] == "entry"
    assert result["data"][const.DATA_ADMIN_TOKEN] == "administrator-token"
    assert result["options"][const.OPT_RUNTIME_MODE] == const.RUNTIME_LEGACY
    assert result["options"][const.OPT_WEBHOOK_AUTH] == const.WEBHOOK_AUTH_NONE
    assert result["options"][const.OPT_BIND_HOST] == const.BIND_HOST_ALL
    assert const.DATA_NATIVE_OWNER_ID not in result["data"]


async def test_old_core_setup_still_shows_the_credential_and_network_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cf, "HA_VERSION", "2026.8.0")
    form = await _make_flow().async_step_server(None)
    fields = {marker.schema for marker in form["data_schema"].schema}
    assert cf.SETUP_ADMIN_TOKEN in fields
    assert const.OPT_BIND_HOST in fields
    assert const.DATA_NATIVE_OWNER_ID not in fields


def test_old_core_assist_returns_the_payload_without_modern_result_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_mcp_tools import llm_tool_metadata as metadata

    monkeypatch.delattr(metadata.llm, "ToolResult")
    payload = {"content": [{"type": "text", "text": "failure detail"}], "isError": True}
    assert metadata.tool_result(payload, error=True) == payload


def test_old_core_assist_can_mirror_annotations_without_modern_metadata_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_mcp_tools import llm_tool_metadata as metadata

    monkeypatch.delattr(metadata.llm, "ToolAnnotations")
    tool = SimpleNamespace()
    metadata.declare_metadata(tool, title="Search", hints={"readOnlyHint": True})
    assert tool.title == "Search"
