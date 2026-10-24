"""Pinned Core group owners never replay writes or overwrite callback replacements."""

import asyncio
import sys
from functools import partial
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext

from .test_native_engine_backend import native as native


class _CoreGroup:
    def __init__(
        self, world, object_id, *, name=None, members=(), icon=None, mode=False
    ):
        self.world = world
        self.entity_id = "group." + object_id
        self.name = name if name is not None else object_id
        self.tracking = tuple(members)
        self.icon = icon
        self.mode = all if mode else any
        self.created_by_service = True
        self.extra_state_attributes = {"order": 0}
        self.context = None

    @staticmethod
    def async_create_group_entity(
        hass: Any,
        name: str,
        *,
        created_by_service: bool,
        entity_ids: Any,
        icon: str | None,
        mode: bool | None,
        object_id: str,
        order: int | None,
    ) -> Any:
        world = hass.data["group_fixture_world"]
        return _CoreGroup(
            world,
            object_id,
            name=name,
            members=entity_ids or (),
            icon=icon,
            mode=mode,
        )

    @staticmethod
    async def async_create_group(
        hass: Any,
        name: str,
        *,
        created_by_service: bool,
        entity_ids: Any,
        icon: str | None,
        mode: bool | None,
        object_id: str,
        order: int | None,
    ) -> Any:
        """Core August's factory has no context keyword."""
        owner = _CoreGroup.async_create_group_entity(
            hass,
            name,
            created_by_service=created_by_service,
            entity_ids=entity_ids,
            icon=icon,
            mode=mode,
            object_id=object_id,
            order=order,
        )
        await hass.data["native-group-component"].async_add_entities([owner])
        return owner

    def async_set_context(self, context):
        self.context = context

    def async_update_tracked_entity_ids(self, members):
        self.tracking = tuple(dict.fromkeys(members))
        self._changed("entities")

    def set_name(self, value):
        self.name = value
        self._changed("name")

    def set_icon(self, value):
        self.icon = value
        self._changed("icon")

    def async_update_group_state(self):
        self.world.calls.append("aggregate")

    def async_write_ha_state(self):
        self.world.states[self.entity_id] = {
            "entity_id": self.entity_id,
            "state": "on",
            "attributes": {
                "friendly_name": self.name,
                "entity_id": list(self.tracking),
                "icon": self.icon,
            },
        }
        self._changed("publish")

    def _changed(self, field):
        self.world.calls.append(field)
        if self.world.callback is not None:
            self.world.callback(self, field)


class _CoreComponent:
    def __init__(self, world):
        self.world = world

    def get_entity(self, entity_id):
        return self.world.rows.get(entity_id)

    async def async_add_entities(self, entities: list[Any]) -> None:
        world = self.world
        world.calls.append("create")
        world.create_started.set()
        if world.block_create:
            await world.create_release.wait()
        for owner in entities:
            world.rows[owner.entity_id] = owner
            world.initial_contexts.append(owner.context)
            owner.async_write_ha_state()
        if world.fail_create:
            if isinstance(world.fail_create, BaseException):
                raise world.fail_create
            raise RuntimeError("Core raised after group creation")

    async def async_remove_entity(self, entity_id):
        self.world.calls.append("remove")
        self.world.rows.pop(entity_id, None)
        self.world.states.pop(entity_id, None)
        if self.world.fail_remove:
            if isinstance(self.world.fail_remove, BaseException):
                raise self.world.fail_remove
            raise RuntimeError("Core raised after group deletion")


@pytest.fixture
def groups(native, monkeypatch):
    from custom_components.ha_mcp_tools.native_group_tools import build_group_tools

    monkeypatch.setattr(sys.modules["homeassistant.core"], "Context", SimpleNamespace)

    world = SimpleNamespace(
        rows={},
        states={},
        calls=[],
        initial_contexts=[],
        callback=None,
        fail_create=False,
        fail_remove=False,
        backup=None,
        block_create=False,
        create_started=asyncio.Event(),
        create_release=asyncio.Event(),
    )
    component = _CoreComponent(world)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.group.const",
        SimpleNamespace(DATA_COMPONENT="native-group-component"),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.group.entity",
        SimpleNamespace(Group=_CoreGroup),
    )

    def validate(payload):
        if "object_id" not in payload or not payload["object_id"]:
            raise ValueError("invalid slug")
        return payload.copy()

    async def executor(function, *arguments):
        return await asyncio.to_thread(function, *arguments)

    async def capture(payload, actor):
        if world.backup is not None:
            await world.backup(payload, actor)

    def all_states(domain):
        return [
            SimpleNamespace(as_dict=lambda value=row: value)
            for row in world.states.values()
            if row["entity_id"].startswith(domain + ".")
        ]

    hass = SimpleNamespace(
        data={"native-group-component": component, "group_fixture_world": world},
        states=SimpleNamespace(get=world.states.get, async_all=all_states),
        services=SimpleNamespace(
            async_services=lambda: {
                "group": {
                    action: SimpleNamespace(schema=validate)
                    for action in ("set", "remove")
                }
            }
        ),
        async_add_executor_job=executor,
    )
    manager = SimpleNamespace(
        hass=hass,
        backend=SimpleNamespace(authorize=AsyncMock()),
        snapshots=SimpleNamespace(group_backup=capture),
    )
    engine = Engine(manager.backend, build_group_tools(manager))
    return (world, manager, engine, partial(_CoreGroup, world))


async def test_native_create_and_list_use_real_owner_values_without_mutating_state(
    groups,
):
    world, _, engine, _ = groups
    result = await engine.call(
        "ha_config_set_group",
        {"object_id": "native", "entities": ["light.a"], "all_on": True, "wait": False},
        CallContext("actor"),
    )
    assert not result.is_error and world.rows["group.native"].context.user_id == "actor"
    assert world.initial_contexts[0].user_id == "actor"
    result = await engine.call("ha_config_list_groups", {}, CallContext("actor"))
    assert result.data["groups"][0]["all"] is True
    assert "all" not in world.states["group.native"]["attributes"]
    assert world.calls.count("create") == 1


async def test_native_compound_update_uses_core_callbacks_and_readback(groups):
    world, _, engine, Group = groups
    owner = Group("native", members=["light.a"])
    world.rows[owner.entity_id] = owner
    result = await engine.call(
        "ha_config_set_group",
        {
            "object_id": "native",
            "add_entities": ["light.b"],
            "name": "Updated",
            "icon": "mdi:lightbulb-group",
            "all_on": True,
            "wait": False,
        },
        CallContext("actor"),
    )
    assert not result.is_error and owner.name == "Updated"
    assert set(owner.tracking) == {"light.a", "light.b"} and owner.mode is all
    assert owner.icon == "mdi:lightbulb-group"
    assert world.calls == ["entities", "name", "icon", "aggregate", "publish"]


async def test_group_callback_cannot_replace_owner_then_receive_later_updates(groups):
    world, _, engine, Group = groups
    owner = Group("native", members=["light.a"])
    world.rows[owner.entity_id] = owner
    replacement = Group("native", name="external")

    def replace_owner(changed, field):
        if field == "entities":
            world.rows[changed.entity_id] = replacement

    world.callback = replace_owner
    result = await engine.call(
        "ha_config_set_group",
        {
            "object_id": "native",
            "entities": ["light.b"],
            "name": "Wanted",
            "wait": False,
        },
        CallContext("actor"),
    )
    assert result.is_error and result.status == "outcome_unknown"
    assert replacement.name == "external" and "name" not in world.calls


async def test_callback_changes_unrequested_field_without_later_overwrite(groups):
    world, _, engine, Group = groups
    owner = Group("native", members=["light.a"])
    world.rows[owner.entity_id] = owner

    def changed_name(changed, field):
        if field == "entities":
            changed.name = "external"

    world.callback = changed_name
    result = await engine.call(
        "ha_config_set_group",
        {
            "object_id": "native",
            "entities": ["light.b"],
            "name": "Wanted",
            "wait": False,
        },
        CallContext("actor"),
    )
    assert result.is_error and result.status == "outcome_unknown"
    assert owner.name == "external" and "name" not in world.calls


async def test_policy_revocation_during_capture_prevents_all_callbacks(groups):
    world, _, engine, Group = groups
    world.rows["group.native"] = Group("native")

    async def revoke(payload, actor):
        await asyncio.sleep(0)
        engine.read_only = True

    world.backup = revoke
    result = await engine.call(
        "ha_config_set_group",
        {"object_id": "native", "name": "Wanted"},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "not_started" and (world.calls == [])


@pytest.mark.parametrize("action", ["set", "remove"])
async def test_core_exception_after_effect_is_unknown_and_never_retried(groups, action):
    world, _, engine, Group = groups
    if action == "remove":
        world.rows["group.native"] = Group("native")
        world.fail_remove = True
    else:
        world.fail_create = True
    result = await engine.call(
        "ha_config_" + action + "_group",
        {"object_id": "native", "wait": False},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "outcome_unknown"
    assert world.calls.count("create" if action == "set" else "remove") == 1


async def test_state_occupying_the_name_is_not_overwritten_or_renamed(groups):
    world, _, engine, _ = groups
    world.states["group.native"] = {"entity_id": "group.native", "state": "external"}
    result = await engine.call(
        "ha_config_set_group",
        {"object_id": "native", "entities": ["light.a"]},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "not_started"
    assert world.calls == [] and world.rows == {}


@pytest.mark.parametrize("action", ["set", "remove"])
async def test_core_cancelled_after_effect_is_returned_as_unknown(groups, action):
    world, _, engine, Group = groups
    cancellation = asyncio.CancelledError(
        "Core cancelled its callback after the effect"
    )
    if action == "remove":
        world.rows["group.native"] = Group("native")
        world.fail_remove = cancellation
    else:
        world.fail_create = cancellation
    result = await engine.call(
        "ha_config_" + action + "_group",
        {"object_id": "native", "wait": False},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "outcome_unknown"
    assert world.calls.count("create" if action == "set" else "remove") == 1


async def test_two_cancellations_keep_the_group_lock_until_core_drains(groups):
    from custom_components.ha_mcp_tools.native_operations.groups import NativeGroups

    world, manager, _, _ = groups
    service = NativeGroups(
        manager.hass, CallContext("actor"), authorize=manager.backend.authorize
    )
    world.block_create = True
    first = asyncio.create_task(
        service.set({"object_id": "native", "entities": ["light.a"]}, wait=False)
    )
    await world.create_started.wait()
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    second = asyncio.create_task(
        service.set({"object_id": "native", "name": "second"}, wait=False)
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    kept_lock = not second.done() and world.calls.count("create") == 1
    world.create_release.set()
    with pytest.raises(asyncio.CancelledError):
        _ = await first
    result = await second
    assert kept_lock and result["success"]
    assert (
        world.calls.count("create") == 1 and world.rows["group.native"].name == "second"
    )
