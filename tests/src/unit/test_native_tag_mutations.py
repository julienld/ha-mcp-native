"""Tag validators have registry effects before the normal collection commit."""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext, UnsupportedOperation

from .test_component_helper_collections import FakeRegistry, _hass
from .test_component_ws_search import wsapi
from .test_native_engine_backend import authorize
from .test_native_engine_backend import native as native
from .test_native_helper_mutations import CoreStorage


class DuplicateTag(ValueError):
    pass


class TagStorage(CoreStorage):
    CREATE_SCHEMA = staticmethod(dict)
    UPDATE_SCHEMA = staticmethod(dict)

    def __init__(self):
        super().__init__()
        self.registry_names = {"target": "Original"}
        self.revoke = None

        def generate(value):
            if value in self.data:
                raise DuplicateTag(value)
            return value

        self.id_manager = SimpleNamespace(
            has_id=lambda value: value in self.data, generate_id=generate
        )

    async def _process_create_data(self, fields):
        item_id = fields["tag_id"] or "generated"
        self.registry_names[item_id] = fields.get("name")
        return {
            "id": item_id,
            **{key: value for key, value in fields.items() if key != "tag_id"},
        }

    async def _update_data(self, current, fields):
        self.registry_names[current["id"]] = fields.get("name", current["name"])
        if self.revoke is not None:
            self.revoke.is_active = False
        return {**current, **fields}

    def _get_suggested_id(self, fields):
        return fields["id"]


@pytest.fixture
async def tags(native, monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.tag",
        SimpleNamespace(
            TAG_ID="tag_id",
            TagIDExistsError=DuplicateTag,
            TagStorageCollection=TagStorage,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.helpers.collection",
        SimpleNamespace(
            StorageCollection=CoreStorage,
            DictStorageCollection=CoreStorage,
            CollectionChange=lambda *values: values,
            CHANGE_ADDED="added",
            CHANGE_UPDATED="updated",
        ),
    )
    collection = TagStorage()
    hass = _hass(collection, "tag")
    user = authorize(hass)
    monkeypatch.setattr(wsapi.er, "async_get", lambda hass: FakeRegistry())
    return collection, user, native.NativeBackend(hass)


async def test_duplicate_tag_cannot_rename_the_existing_registry_entry(tags):
    collection, _, backend = tags
    result = await backend.execute(
        "helpers.storage.write",
        {
            "helper_type": "tag",
            "action": "create",
            "data": {"tag_id": "target", "name": "Changed"},
        },
        CallContext("actor"),
    )
    assert not result["success"]
    assert collection.registry_names == {"target": "Original"}
    assert collection.saves == 0


async def test_tag_registry_effect_before_revocation_is_never_reported_not_started(
    tags,
):
    collection, user, backend = tags
    collection.revoke = user
    actor = CallContext("actor")
    engine = Engine(backend)
    result = await engine.call_owned(
        "tag-update",
        lambda: backend.execute(
            "helpers.storage.write",
            {
                "helper_type": "tag",
                "action": "update",
                "item_id": "target",
                "data": {"name": "Changed"},
                "expected_item": dict(collection.data["target"]),
            },
            actor,
        ),
        actor,
        writes=True,
    )
    assert result.is_error and result.status == "outcome_unknown", result
    assert result.data["config_applied"] is True
    assert (
        collection.registry_names["target"]
        == collection.data["target"]["name"]
        == "Changed"
    )
    assert collection.saves == 1


async def test_core_tag_adds_a_registry_entry_and_generates_the_missing_id(tags):
    collection, _, backend = tags
    result = await backend.execute(
        "helpers.storage.write",
        {
            "helper_type": "tag",
            "action": "create",
            "data": {"name": "Generated tag"},
        },
        CallContext("actor"),
    )
    assert result["success"] and result["item"]["id"] == "generated"
    assert collection.registry_names["generated"] == "Generated tag"


async def test_a_future_suspending_tag_validator_is_refused_before_dispatch(
    tags, monkeypatch
):
    collection, _, backend = tags

    async def future_validator(self, current, fields):
        await asyncio.sleep(0)
        self.registry_names[current["id"]] = fields["name"]
        return {**current, **fields}

    monkeypatch.setattr(TagStorage, "_update_data", future_validator)
    with pytest.raises(UnsupportedOperation):
        await backend.execute(
            "helpers.storage.write",
            {
                "helper_type": "tag",
                "action": "update",
                "item_id": "target",
                "data": {"name": "Changed"},
            },
            CallContext("actor"),
        )
    assert collection.registry_names == {"target": "Original"} and collection.saves == 0
