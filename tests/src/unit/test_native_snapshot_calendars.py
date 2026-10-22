"""Calendar history preserves occurrence identity and never guesses a recreation."""

import copy
from typing import Any

import pytest

from ha_mcp.runtime.snapshot_payloads import BackupRestoreError
from ha_mcp.tool_api import CallContext

from .test_native_calendars import _Calendar, _setup
from .test_native_engine_backend import native as native
from .test_native_snapshot_restore import runtime as runtime


@pytest.fixture
def calendars(runtime: Any, native: Any, monkeypatch: pytest.MonkeyPatch) -> tuple:
    manager, snapshots, store, *_ = runtime
    service, entity, entities, _, _ = _setup(native, monkeypatch)
    manager.hass.data = service.hass.data

    async def update(uid: str, event: dict[str, Any], **kwargs: Any) -> None:
        assert uid == "original"
        row = entity.events[0]
        for key in ("summary", "description", "location"):
            setattr(row, key, event[key])
        row.start, row.end = event["dtstart"], event["dtend"]

    entity.async_update_event.side_effect = update
    saved = {
        **vars(entity.events[0]),
        "calendar_entity_id": entity.entity_id,
        "summary": "Saved appointment",
        "description": "Saved description",
    }
    path = store.capture("calendar_event", "calendar.family::original", saved, "fixture")
    return manager, snapshots, store, path, entity, entities


async def test_calendar_restore_uses_same_owner_and_one_exact_safety_image(
    calendars: tuple,
) -> None:
    _manager, snapshots, store, path, entity, _ = calendars
    data, before = await snapshots.snapshot_comparison(path.name, CallContext("owner"))
    result = await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert result["apply_status"] == "applied"
    assert result["result"]["verification_status"] == "matched"
    assert entity.events[0].summary == data["config"]["summary"]
    assert store.read_snapshot(result["safety_backup"])["config"] == before
    assert len(store.list_snapshots(domain="calendar_event")) == 2
    entity.async_update_event.assert_awaited_once()
    assert "rrule" not in entity.async_update_event.await_args.args[1]
    entity.async_create_event.assert_not_awaited()


@pytest.mark.parametrize("change", ["owner", "occurrence"])
async def test_calendar_restore_refuses_change_during_safety_capture(
    calendars: tuple, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    _manager, snapshots, _store, path, entity, entities = calendars
    io = snapshots._io

    async def changed(function: Any, *args: Any) -> Any:
        result = await io(function, *args)
        if function.__name__ == "capture":
            if change == "owner":
                entities[entity.entity_id] = _Calendar()
            else:
                entity.events[0].summary = "Concurrent edit"
        return result

    monkeypatch.setattr(snapshots, "_io", changed)
    with pytest.raises(BackupRestoreError) as error:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert error.value.outcome["apply_status"] == "not_applied"
    assert error.value.outcome["safety_backup"]
    entity.async_update_event.assert_not_awaited()
    entity.async_create_event.assert_not_awaited()


async def test_missing_calendar_uid_returns_saved_values_without_creating_a_duplicate(
    calendars: tuple,
) -> None:
    _manager, snapshots, _store, path, entity, _ = calendars
    entity.events.clear()
    with pytest.raises(BackupRestoreError) as error:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    outcome = error.value.outcome
    assert outcome["apply_status"] == "not_applied"
    assert outcome["reason"] == "calendar_identity_unavailable"
    assert outcome["manual_create_arguments"]["summary"] == "Saved appointment"
    assert "uid" not in outcome["manual_create_arguments"]
    entity.async_update_event.assert_not_awaited()
    entity.async_create_event.assert_not_awaited()


async def test_calendar_restore_failure_after_update_never_falls_back_to_creation(
    calendars: tuple,
) -> None:
    _manager, snapshots, _store, path, entity, _ = calendars
    entity.async_update_event.side_effect = RuntimeError("provider may have written")
    with pytest.raises(BackupRestoreError) as error:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert error.value.outcome["apply_status"] == "unknown"
    entity.async_update_event.assert_awaited_once()
    entity.async_create_event.assert_not_awaited()


async def test_calendar_backup_keeps_the_observed_image_without_rereading(
    calendars: tuple,
) -> None:
    _manager, snapshots, store, _path, entity, _ = calendars
    before = {**vars(entity.events[0]), "calendar_entity_id": entity.entity_id}
    await snapshots.calendar_backup(
        {
            "domain": "calendar_event",
            "backup_id": "calendar.family::original",
            "current_config": copy.deepcopy(before),
            "source_tool": "ha_config_set_calendar_event",
        },
        CallContext("owner"),
    )
    rows = store.list_snapshots(domain="calendar_event")
    assert any(store.read_snapshot(row["name"])["config"] == before for row in rows)
    entity.async_get_events.assert_not_awaited()


async def test_calendar_history_keeps_a_provider_uid_containing_the_key_separator(
    calendars: tuple,
) -> None:
    _manager, snapshots, store, _path, entity, _ = calendars
    entity.events[0].uid = "provider::event"
    saved = {**vars(entity.events[0]), "calendar_entity_id": entity.entity_id}
    path = store.capture(
        "calendar_event", "calendar.family::provider::event", saved, "fixture"
    )
    _data, current = await snapshots.snapshot_comparison(path.name, CallContext("owner"))
    assert current["uid"] == "provider::event"
