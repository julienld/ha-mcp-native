"""Native panel controls use the same live File/YAML entry and permission store."""

import asyncio
import importlib
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tool_api import CallContext

from .test_native_engine_backend import native as native
from .test_native_file_access import files as files
from .test_native_filesystem import filesystem as filesystem


@pytest.fixture
def panel(filesystem, monkeypatch):
    _files, manager, owner, _target = filesystem
    from ._embedded_stubs import HomeAssistantView, _make_fake_web

    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.http",
        SimpleNamespace(HomeAssistantView=HomeAssistantView),
    )
    from custom_components.ha_mcp_tools.native_settings import NativeSettings

    web = _make_fake_web()
    monkeypatch.setattr("custom_components.ha_mcp_tools.native_settings.web", web)
    monkeypatch.setattr("custom_components.ha_mcp_tools.native_settings_files.web", web)

    manager.is_running = True
    manager.refresh_policy = AsyncMock()
    monkeypatch.setattr(
        "custom_components.ha_mcp_tools.native_settings_files.settings_context",
        AsyncMock(return_value=CallContext("owner")),
    )
    return NativeSettings(manager), owner


async def test_panel_paths_persist_and_apply_without_restart(panel, monkeypatch):
    settings, owner = panel
    persisted = owner.config_dir / "persisted-permissions.json"

    async def save(_hass, paths):
        await asyncio.to_thread(persisted.write_text, json.dumps({"paths": paths}))

    component = importlib.import_module("custom_components.ha_mcp_tools")
    monkeypatch.setattr(component, "_save_allowed_paths", save)
    request = SimpleNamespace(
        method="POST",
        json=AsyncMock(return_value={"paths": ["notes", ".storage", "notes"]}),
    )
    result = await settings.handle(request, "api/settings/fs-custom-paths")
    data = result.json_body
    assert (
        result.status == 200
        and data["paths"] == ["notes"]
        and data["rejected"] == [".storage"]
    )
    assert data["restart_required"] is False
    assert json.loads(persisted.read_text()) == {"paths": ["notes"]}
    view = await settings.handle(
        SimpleNamespace(method="GET"), "api/settings/fs-custom-paths"
    )
    assert view.json_body["available"] and view.json_body["paths"] == ["notes"]
    assert not owner.pending


async def test_panel_paths_revalidate_administrator_after_path_resolution(
    panel, monkeypatch
):
    settings, owner = panel
    component = importlib.import_module("custom_components.ha_mcp_tools")
    paths = importlib.import_module("custom_components.ha_mcp_tools.file_paths")
    original = paths.normalize_allowed_paths

    def revoke(*args):
        result = original(*args)
        settings.manager.backend.authorize.side_effect = PermissionError("revoked")
        return result

    saved = AsyncMock()
    monkeypatch.setattr(component, "_save_allowed_paths", saved)
    monkeypatch.setattr(paths, "normalize_allowed_paths", revoke)
    request = SimpleNamespace(
        method="POST", json=AsyncMock(return_value={"paths": ["notes"]})
    )
    response = await settings.handle(request, "api/settings/fs-custom-paths")
    assert response.status == 403
    saved.assert_not_awaited()
    assert owner.hass.data["ha_mcp_tools"]["allowed_paths"] == []


def test_panel_file_yaml_controls_preserve_environment_precedence(panel, monkeypatch):
    settings, _owner = panel
    from custom_components.ha_mcp_tools.native_server_policy import load_policy

    for key in ("ENABLE_YAML_CONFIG_EDITING", "HA_MCP_EXTRA_YAML_KEYS"):
        monkeypatch.delenv(key, raising=False)
    saved = settings._save_features(
        {
            "flags": {
                "enable_filesystem_tools": True,
                "enable_yaml_config_editing": True,
                "enable_yaml_packages_automation": False,
            }
        }
    )
    assert saved["restart_required"]
    settings._save_advanced({"extra_yaml_write_keys": "custom_domain, alert2"})
    policy = load_policy(settings.directory)
    assert (
        policy.flags["enable_yaml_config_editing"]
        and policy.flags["enable_filesystem_tools"]
    )
    assert policy.flags["extra_yaml_write_keys"] == "custom_domain, alert2"
    monkeypatch.setenv("HA_MCP_EXTRA_YAML_KEYS", "locked")
    with pytest.raises(ValueError, match="Environment-controlled"):
        settings._save_advanced({"extra_yaml_write_keys": "changed"})
