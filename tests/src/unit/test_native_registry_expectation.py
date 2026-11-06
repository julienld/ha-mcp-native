"""A recovery image covers the registry fields restored with the stored item."""

import asyncio
from types import SimpleNamespace

import pytest

from ha_mcp.tool_api import CallContext, ToolFailure

from .test_component_ws_search import wsapi
from .test_native_engine_backend import native as native
from .test_native_helper_mutations import storage as storage
from .test_native_tag_mutations import tags as tags


def registry(monkeypatch, platform):
    row = SimpleNamespace(
        entity_id=f"{platform}.target",
        id="row",
        platform=platform,
        unique_id="target",
        icon="mdi:old",
        name="Original",
    )
    owner = SimpleNamespace(
        async_get_entity_id=lambda *args: row.entity_id, async_get=lambda _: row
    )
    monkeypatch.setattr(wsapi.er, "async_get", lambda _: owner)
    expected = {
        "entity_id": row.entity_id,
        "id": row.id,
        "platform": platform,
        "unique_id": "target",
        "values": {"icon": row.icon},
    }
    return row, expected


async def test_registry_edit_during_storage_validation_refuses_restore_before_commit(
    storage, monkeypatch
):
    collection, _user, backend, engine = storage
    row, expected = registry(monkeypatch, "person")
    actor = CallContext("actor")
    task = asyncio.create_task(
        engine.call_owned(
            "restore",
            lambda: backend.execute(
                "helpers.storage.write",
                {
                    "helper_type": "person",
                    "action": "update",
                    "item_id": "target",
                    "data": {"name": "Restored"},
                    "expected_item": dict(collection.data["target"]),
                    "expected_registry": expected,
                },
                actor,
            ),
            actor,
            writes=True,
        )
    )
    await asyncio.wait_for(collection.validating.wait(), 2)
    row.icon = "mdi:human"
    collection.validated.set()
    result = await task
    assert result.is_error and result.status == "not_started"
    assert result.data["error"]["code"] == "CONFIG_CONFLICT"
    assert collection.data["target"]["name"] == "Original"
    assert collection.saves == 0


async def test_changed_tag_registry_is_refused_before_side_effecting_validator(
    tags, monkeypatch
):
    collection, _user, backend = tags
    row, expected = registry(monkeypatch, "tag")
    row.id = "replacement-row"
    with pytest.raises(ToolFailure) as caught:
        await backend.execute(
            "helpers.storage.write",
            {
                "helper_type": "tag",
                "action": "update",
                "item_id": "target",
                "data": {"name": "Restored"},
                "expected_item": dict(collection.data["target"]),
                "expected_registry": expected,
            },
            CallContext("actor"),
        )
    assert caught.value.status == "not_started"
    assert collection.registry_names["target"] == "Original"
    assert collection.saves == 0
