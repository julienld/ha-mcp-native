"""File/YAML history retains safety images and uses the actual component writer."""

import pytest

from ha_mcp.runtime.snapshot_payloads import BackupRestoreError
from ha_mcp.tool_api import CallContext

from .test_native_engine_backend import native as native
from .test_native_file_access import files as files
from .test_native_filesystem import filesystem as filesystem
from .test_native_yaml import yaml_files as yaml_files


@pytest.mark.parametrize("domain", ["file", "yaml", "yaml_file"])
async def test_native_file_restore_preserves_safety_when_automatic_captures_are_off(
    yaml_files, domain
):
    _edit, _read, manager, config_file = yaml_files
    store = manager.snapshots._store()
    manager.policy.flags.update(backup_read_only=False, enable_auto_backup=False)
    target = config_file
    identifier = "configuration.yaml"
    saved = "utility_meter:\n  restored:\n    source: sensor.saved\n"
    if domain == "file":
        target = config_file.parent / "www" / "notes.txt"
        identifier, saved = "www/notes.txt", "saved plain text"
    elif domain == "yaml":
        identifier += "::utility_meter"
        saved = "restored:\n  source: sensor.saved\n"
    path = store.capture(domain, identifier, saved, "fixture")
    data, before = await manager.snapshots.snapshot_comparison(
        path.name, CallContext("owner")
    )
    assert data["config"] == saved and before is not None
    result = await manager.snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert result["apply_status"] == "applied" and result["safety_backup"]
    assert store.read_snapshot(result["safety_backup"])["config"] == before
    assert "saved" in target.read_text()
    assert len(store.list_snapshots(domain=domain)) == 2


@pytest.mark.parametrize("change", ["target", "entry", "actor"])
async def test_native_file_restore_does_not_rebase_after_safety_capture(
    yaml_files, monkeypatch, change
):
    _edit, _read, manager, config_file = yaml_files
    manager.policy.flags["backup_read_only"] = False
    store = manager.snapshots._store()
    path = store.capture(
        "yaml", "configuration.yaml::utility_meter", "restored: {}\n", "fixture"
    )
    capture = store.capture
    captured = []

    def safety(*args, **kwargs):
        result = capture(*args, **kwargs)
        captured.append(True)
        if change == "target":
            config_file.write_text("utility_meter:\n  external: {}\n")
        elif change == "entry":
            manager.hass.data["ha_mcp_tools"].pop("file_access_owner")
        else:
            manager.backend.authorize.side_effect = PermissionError("revoked")
        return result

    monkeypatch.setattr(store, "capture", safety)
    with pytest.raises(BackupRestoreError) as raised:
        await manager.snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert captured == [True]
    assert raised.value.outcome["apply_status"] == "not_applied"
    assert "restored" not in config_file.read_text()


async def test_file_backup_target_cannot_read_unmasked_root_secrets(yaml_files):
    _edit, _read, manager, config_file = yaml_files
    target = config_file.parent / "secrets.yaml"
    target.write_text("secret: do-not-disclose\n")
    path = manager.snapshots._store().capture(
        "file", "secrets.yaml", "saved", "fixture"
    )
    from ha_mcp.tool_api import ToolFailure

    with pytest.raises(ToolFailure, match="not allowed"):
        await manager.snapshots.snapshot_comparison(path.name, CallContext("owner"))
