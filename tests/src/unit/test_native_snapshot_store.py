"""Native captures remain readable and safely retained by legacy tooling."""

from types import SimpleNamespace

import pytest

from ha_mcp.backup_manager import BackupManager
from ha_mcp.runtime.snapshot_files import SnapshotInUseError
from ha_mcp.runtime.snapshot_store import SnapshotStore


def test_native_capture_is_a_legacy_snapshot_with_registry_metadata(tmp_path):
    native = SnapshotStore(tmp_path)
    item = {"id": "night", "name": "Night", "registry_icon": "mdi:moon"}
    path = native.capture(
        "helper_input_boolean", "input_boolean.night", item, "ha_config_set_helper"
    )
    legacy = BackupManager(SimpleNamespace(auto_backup_dir=str(tmp_path)), None)
    record = legacy.read_snapshot(path.name)
    assert record["schema_version"] == 1
    assert record["entity_id"] == "input_boolean.night"
    assert record["config"] == item
    assert (
        legacy.list_snapshots(entity_id="input_boolean.night")[0]["name"] == path.name
    )


def test_native_same_second_capture_never_overwrites_and_retention_keeps_latest(
    tmp_path,
):
    native = SnapshotStore(tmp_path, retain_per_entity=2)
    native._snapshot_now_ts = lambda: "20261022_010000"
    paths = [
        native.capture(
            "helper_input_text", "a/b", {"value": str(i)}, "test", force=True
        )
        for i in range(3)
    ]
    assert len({p.name for p in paths}) == 3
    assert not paths[0].exists()
    assert all(p.exists() for p in paths[1:])
    assert native.read_snapshot(paths[2].name)["config"]["value"] == "2"


def test_native_throttle_and_payload_identity_preserve_unrelated_history(tmp_path):
    native = SnapshotStore(tmp_path, retain_per_entity=1, throttle_seconds=120)
    first = native.capture("helper_input_text", "a/b", {"value": "one"}, "test")
    other = native.capture("helper_input_text", "a_b", {"value": "other"}, "test")
    assert native.capture("helper_input_text", "a/b", {"value": "two"}, "test") is None
    latest = native.capture(
        "helper_input_text", "a/b", {"value": "three"}, "test", force=True
    )
    assert not first.exists()
    assert other.exists() and latest.exists()


def test_rotation_failure_does_not_hide_an_existing_native_capture(
    tmp_path, monkeypatch
):
    native = SnapshotStore(tmp_path)

    def unavailable(*args):
        raise OSError("retention directory unavailable")

    monkeypatch.setattr(native, "_rotate", unavailable)
    path = native.capture("helper_input_boolean", "night", {"name": "Night"}, "test")
    assert path.is_file()


def test_capture_preserves_a_restore_pin_acquired_during_rotation(
    tmp_path, monkeypatch
):
    native = SnapshotStore(tmp_path)

    def begin_restore(domain, entity_id, protected):
        for name in protected:
            native._protect_snapshot(name)

    monkeypatch.setattr(native, "_rotate", begin_restore)
    path = native.capture("helper_input_boolean", "night", {}, "test")
    with pytest.raises(SnapshotInUseError):
        native.delete_snapshot(path.name)
    native._unprotect_snapshot(path.name)
    assert native.delete_snapshot(path.name) == path
