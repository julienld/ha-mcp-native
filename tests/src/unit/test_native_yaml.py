"""Native YAML uses real component parsing, exact snapshots and live write gates."""

import importlib
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_engine_backend import native as native
from .test_native_file_access import files as files
from .test_native_filesystem import filesystem as filesystem


@pytest.fixture
def yaml_files(filesystem, monkeypatch):
    from custom_components.ha_mcp_tools.native_operations.yaml_config import (
        NativeYamlConfig,
        NativeYamlRead,
    )

    _files, manager, owner, _target = filesystem
    for key in (
        "ENABLE_YAML_CONFIG_EDITING",
        "ENABLE_YAML_EDIT_CONFIRM",
        "ENABLE_MANDATORY_BPS",
    ):
        monkeypatch.delenv(key, raising=False)
    manager.policy.flags.update(
        enable_yaml_config_editing=True,
        enable_yaml_edit_confirm=True,
        enable_mandatory_bps=False,
    )
    module = importlib.import_module("custom_components.ha_mcp_tools.yaml_editing")
    monkeypatch.setattr(
        module, "async_check_ha_config_file", AsyncMock(return_value=None)
    )
    target = owner.config_dir / "configuration.yaml"
    target.write_text(
        "# retained\nautomation: !include automations.yaml\nutility_meter:\n  original:\n    source: sensor.before\n"
    )
    actor = CallContext("owner")
    return (
        NativeYamlConfig(manager, actor),
        NativeYamlRead(manager, actor),
        manager,
        target,
    )


async def test_native_yaml_preview_has_no_backup_then_confirm_captures_subtree(
    yaml_files,
):
    edit, read, manager, target = yaml_files
    before = target.read_text()
    preview = await edit.edit(
        "utility_meter",
        "replace",
        "requested:\n  source: sensor.after\n",
        MandatoryBPS=False,
    )
    assert preview["preview"] and target.read_text() == before
    assert not manager.snapshots._store().list_snapshots(domain="yaml")
    saved = await edit.edit(
        "utility_meter",
        "replace",
        "requested:\n  source: sensor.after\n",
        confirm_token=preview["confirm_token"],
        MandatoryBPS=False,
    )
    assert saved["written"] and saved["config_check"] == "ok"
    assert (
        "# retained" in target.read_text()
        and "!include automations.yaml" in target.read_text()
    )
    store = manager.snapshots._store()
    rows = store.list_snapshots(domain="yaml")
    assert len(rows) == 1
    snapshot = store.read_snapshot(rows[0]["name"])
    assert snapshot["entity_id"] == "configuration.yaml::utility_meter"
    assert (
        "sensor.before" in snapshot["config"] and "!include" not in snapshot["config"]
    )
    result = await read.get(yaml_path="utility_meter", include_parsed=True)
    assert result["matches"][0]["parsed"] == {"requested": {"source": "sensor.after"}}


@pytest.mark.parametrize("change", ["target", "key", "actor", "backup"])
async def test_native_yaml_change_during_safety_capture_never_rebases_or_writes(
    yaml_files, change
):
    edit, _read, manager, target = yaml_files
    manager.policy.flags.update(
        enable_yaml_edit_confirm=False, extra_yaml_write_keys="custom_domain"
    )
    target.write_text("custom_domain:\n  source: sensor.before\n")
    capture = manager.snapshots.file_backup
    captured = []

    async def capture_then_change(*args):
        await capture(*args)
        captured.append(True)
        if change == "target":
            target.write_text("custom_domain:\n  source: sensor.external\n")
        elif change == "key":
            manager.policy.flags["extra_yaml_write_keys"] = ""
        elif change == "actor":
            manager.backend.authorize.side_effect = PermissionError("revoked")
        else:
            manager.policy.flags["enable_auto_backup"] = False

    manager.snapshots.file_backup = capture_then_change
    with pytest.raises(ToolFailure) as raised:
        await edit.edit(
            "custom_domain", "replace", "source: sensor.after\n", MandatoryBPS=False
        )
    assert captured == [True] and raised.value.status == "not_started"
    assert "sensor.after" not in target.read_text()


async def test_native_yaml_read_glob_uses_shared_lookup_and_never_resolves_secrets(
    yaml_files,
):
    _edit, read, _manager, target = yaml_files
    package = target.parent / "packages"
    package.mkdir()
    (package / "one.yaml").write_text("mqtt:\n  password: !secret mqtt_password\n")
    (package / "bad.yaml").write_text("mqtt: [unterminated\n")
    result = await read.get(
        yaml_path="mqtt", file="packages/*.yaml", include_parsed=True
    )
    assert result["files_searched"] == 2 and result["files_unreadable"] == 1
    assert result["matches"][0]["parsed"] == {"password": "!secret mqtt_password"}


@pytest.mark.parametrize(
    "error",
    [
        "plain component failure",
        {"code": "CONFIG_INVALID", "message": "nested failure"},
    ],
)
def test_shared_error_preserves_string_and_structured_component_payloads(error):
    from ha_mcp.runtime.errors import raise_failure

    payload = {"success": False, "error": error}
    with pytest.raises(ToolFailure) as raised:
        raise_failure(payload)
    assert raised.value.data == payload
    assert raised.value.code == (
        error["code"] if isinstance(error, dict) else "TOOL_ERROR"
    )
    assert str(raised.value) == (error["message"] if isinstance(error, dict) else error)


async def test_native_yaml_theme_reload_carries_the_actual_actor_context(
    yaml_files, monkeypatch
):
    from types import SimpleNamespace

    core = importlib.import_module("homeassistant.core")

    edit, _read, manager, _target = yaml_files
    monkeypatch.setattr(core, "Context", SimpleNamespace)
    manager.policy.flags["enable_yaml_edit_confirm"] = False
    reload = AsyncMock()
    manager.hass.services.async_call = reload
    result = await edit.edit(
        "native-theme",
        "add",
        "primary-color: '#ffffff'\n",
        file="themes/native.yaml",
        MandatoryBPS=False,
    )
    assert result["post_action"] == "reload_performed" and result["written"]
    assert reload.call_args.args[:2] == ("frontend", "reload_themes")
    assert reload.call_args.kwargs["context"].user_id == "owner"
