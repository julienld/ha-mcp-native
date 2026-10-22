"""Direct calendar calls preserve Core validation, targets and dispatch outcomes."""

import asyncio
import copy
import importlib
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntFlag
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_engine_backend import native as native


@dataclass
class _Event:
    uid: str = "original"
    summary: str = "Before"
    start: str = "2026-06-15T10:00:00+00:00"
    end: str = "2026-06-15T11:00:00+00:00"
    recurrence_id: str | None = None
    rrule: str | None = None
    future_core_field: str = "retained"


class _Features(IntFlag):
    CREATE_EVENT = 1
    UPDATE_EVENT = 2
    DELETE_EVENT = 4


class _Calendar:
    entity_id = "calendar.family"
    unique_id = "provider-calendar-id"
    supported_features = _Features(7)

    def __init__(self) -> None:
        self.events = [_Event()]
        self.async_get_events = AsyncMock(side_effect=self._events)
        self.async_create_event = AsyncMock()
        self.async_update_event = AsyncMock()
        self.async_delete_event = AsyncMock()

    async def _events(self, *args: Any) -> list[_Event]:
        return copy.deepcopy(self.events)


def _setup(native: Any, monkeypatch: pytest.MonkeyPatch, **options: Any) -> tuple:
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.calendar_events"
    )
    entity = _Calendar()
    entities = {entity.entity_id: entity}
    component = SimpleNamespace(get_entity=entities.get)
    hass = SimpleNamespace(data={"calendar_component": component})
    validator = Mock(side_effect=lambda value: {**value, "from_core_validator": True})
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.calendar",
        SimpleNamespace(
            DATA_COMPONENT="calendar_component",
            CalendarEntity=_Calendar,
            CalendarEntityFeature=_Features,
            WEBSOCKET_EVENT_SCHEMA=validator,
            api_event_dict_factory=dict,
        ),
    )
    clock = datetime(2026, 6, 15, tzinfo=UTC)
    dt_util = SimpleNamespace(
        now=lambda: clock,
        utcnow=lambda: clock,
        parse_datetime=datetime.fromisoformat,
        as_local=lambda value: value,
    )
    monkeypatch.setattr(sys.modules["homeassistant.util"], "dt", dt_util, raising=False)
    authorize = AsyncMock()
    service = module.NativeCalendars(
        hass, CallContext("actor"), authorize=authorize, **options
    )
    return service, entity, entities, validator, authorize


async def test_calendar_read_uses_live_entity_and_keeps_core_event_fields(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, entity, _, _, _ = _setup(native, monkeypatch)
    result = await service.get("calendar.family")
    assert result["events"][0]["future_core_field"] == "retained"
    assert result["events"][0]["uid"] == "original"
    assert result["count"] == result["total_available"] == 1
    assert entity.async_get_events.await_args.args[0] is service.hass
    assert entity.async_get_events.await_args.args[1].tzinfo is not None


async def test_calendar_creation_forwards_actual_core_validator_result(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, entity, _, validator, _ = _setup(native, monkeypatch)
    result = await service.set("calendar.family", "Meeting", "2026-06-15", "2026-06-16")
    assert result["success"]
    validator.assert_called_once()
    assert entity.async_create_event.await_args.kwargs["from_core_validator"] is True
    assert entity.async_create_event.await_args.kwargs["dtstart"] == "2026-06-15"
    entity.async_get_events.assert_not_awaited()


async def test_calendar_update_captures_original_occurrence_and_clears_text(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = AsyncMock(return_value={"warnings": ["captured"]})
    service, entity, _, _, _ = _setup(native, monkeypatch, backup=backup)
    result = await service.set(
        "calendar.family", "After", "2026-06-15", "2026-06-16", uid="original"
    )
    before = backup.await_args.args[0]
    assert before["backup_id"] == "calendar.family::original"
    assert before["current_config"]["summary"] == "Before"
    assert before["current_config"]["future_core_field"] == "retained"
    event = entity.async_update_event.await_args.args[1]
    assert event["summary"] == "After"
    assert event["description"] == event["location"] == ""
    assert result["warnings"] == ["captured"]


@pytest.mark.parametrize("change", ["owner", "occurrence", "credentials"])
async def test_calendar_write_refuses_changes_during_snapshot_io(
    native: Any, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    service, entity, entities, _, authorize = _setup(native, monkeypatch)

    async def backup(*args: Any) -> None:
        if change == "owner":
            entities[entity.entity_id] = _Calendar()
        elif change == "occurrence":
            entity.events[0].summary = "Concurrent edit"
        else:
            authorize.side_effect = PermissionError("revoked")

    service.backup = backup
    with pytest.raises(ToolFailure) as error:
        await service.remove("calendar.family", "original")
    assert error.value.code == (
        "AUTH_FORBIDDEN" if change == "credentials" else "CONFIG_CONFLICT"
    )
    entity.async_delete_event.assert_not_awaited()


@pytest.mark.parametrize("action", ["create", "update", "delete"])
async def test_calendar_provider_failure_after_dispatch_is_unknown_without_retry(
    native: Any, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    service, entity, _, _, _ = _setup(native, monkeypatch)
    operation = getattr(entity, f"async_{action}_event")
    operation.side_effect = RuntimeError("provider stored event, then failed")
    with pytest.raises(ToolFailure) as error:
        if action == "delete":
            await service.remove("calendar.family", "original")
        else:
            await service.set(
                "calendar.family",
                "After",
                "2026-06-15",
                "2026-06-16",
                uid="original" if action == "update" else None,
            )
    assert error.value.code == "OUTCOME_UNKNOWN"
    assert error.value.status == "outcome_unknown"
    assert operation.await_count == 1


async def test_calendar_range_targets_exact_occurrence_without_claiming_full_backup(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = AsyncMock()
    service, entity, _, _, _ = _setup(native, monkeypatch, backup=backup)
    result = await service.remove(
        "calendar.family", "original", "20260615T100000", "THISANDFUTURE"
    )
    assert result["warnings"]
    backup.assert_not_awaited()
    entity.async_get_events.assert_not_awaited()
    assert entity.async_delete_event.await_args.kwargs == {
        "recurrence_id": "20260615T100000",
        "recurrence_range": "THISANDFUTURE",
    }


async def test_calendar_cancellation_drains_the_dispatched_provider_call(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, entity, _, _, _ = _setup(native, monkeypatch)
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def create(**kwargs: Any) -> None:
        entered.set()
        await release.wait()
        finished.set()

    entity.async_create_event.side_effect = create
    task = asyncio.create_task(
        service.set("calendar.family", "Meeting", "2026-06-15", "2026-06-16")
    )
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


async def test_unavailable_calendar_snapshot_does_not_block_a_qualified_write(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = AsyncMock()
    service, entity, _, _, _ = _setup(native, monkeypatch, backup=backup)
    entity.async_get_events.side_effect = NotImplementedError("write-only provider")
    result = await service.remove("calendar.family", "original")
    assert result["success"] and result["warnings"]
    entity.async_delete_event.assert_awaited_once()
    backup.assert_not_awaited()


async def test_calendar_reread_failure_after_capture_refuses_the_write(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = AsyncMock(return_value=None)
    service, entity, _, _, _ = _setup(native, monkeypatch, backup=backup)
    entity.async_get_events.side_effect = [
        copy.deepcopy(entity.events),
        RuntimeError("calendar unavailable after capture"),
    ]
    with pytest.raises(ToolFailure):
        await service.remove("calendar.family", "original")
    backup.assert_awaited_once()
    entity.async_delete_event.assert_not_awaited()


async def test_calendar_catalog_binds_the_direct_owner_and_engine_write_policy(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, entity, _, _, authorize = _setup(native, monkeypatch)
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_calendar_tools"
    )
    backend = SimpleNamespace(authorize=authorize, execute=AsyncMock())
    manager = SimpleNamespace(
        hass=service.hass,
        backend=backend,
        snapshots=SimpleNamespace(calendar_backup=AsyncMock(), _value=lambda name: 7),
    )
    engine = Engine(backend, module.build_calendar_tools(manager))
    arguments = {
        "entity_id": "calendar.family",
        "summary": "Created",
        "start": "2026-06-15",
        "end": "2026-06-16",
    }
    with pytest.raises(ToolFailure) as refused:
        await engine.call(
            "ha_config_set_calendar_event", arguments, CallContext("actor", read_only=True)
        )
    assert refused.value.code == "READ_ONLY"
    entity.async_create_event.assert_not_awaited()
    created = await engine.call(
        "ha_config_set_calendar_event", arguments, CallContext("actor")
    )
    assert not created.is_error, created.data
    entity.async_create_event.assert_awaited_once()
    backend.execute.assert_not_awaited()
