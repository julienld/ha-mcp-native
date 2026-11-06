"""A reused internal automation owner keeps each write's lock image local.

The production native catalog constructs an owner per invocation. These tests
deliberately reuse one owner through Engine admission; they do not establish a
public native-catalog concurrency failure.
"""

import asyncio
import copy
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime.automation_writes import AutomationWriteTools
from ha_mcp.tool_api import CallContext
from ha_mcp.utils.config_hash import compute_config_hash

from .test_native_catalog_contract_automations import _setup_automation
from .test_native_catalog_contract_config import (
    _real_aiohttp_router as _real_aiohttp_router,
)
from .test_native_engine_backend import native as native


def _reused_owner(native, monkeypatch):
    backup = AsyncMock(return_value={})
    engine, owner, _, _, writes, tasks = _setup_automation(
        native, monkeypatch, backup=backup
    )
    tools = AutomationWriteTools(engine.backend, CallContext("actor"))

    async def invoke(arguments, context):
        return await tools.ha_config_set_automation(**arguments)

    name = "ha_config_set_automation"
    engine.tools[name] = replace(engine.tools[name], handler=invoke)
    return engine, tools, owner, backup, writes, tasks


def _arguments(before, mode="hash"):
    arguments = {
        "identifier": before["id"],
        "wait": False,
        "MandatoryBPS": False,
    }
    if mode != "alias":
        # Public hashes describe the normalized config; CAS must keep its raw
        # singular keys even while a transform edits the normalized copy.
        from ha_mcp.runtime.config_normalize import _normalize_config_for_roundtrip

        arguments["config_hash"] = compute_config_hash(
            _normalize_config_for_roundtrip(before)
        )
    if mode == "transform":
        arguments["python_transform"] = "config['actions'][0]['delay'] = 2"
    else:
        arguments["config"] = {
            "alias": before["alias"],
            "triggers": [],
            "actions": [{"delay": 2}],
        }
    return arguments


def _pause_first_validation(tools, monkeypatch):
    verified = asyncio.Event()
    resume = asyncio.Event()
    validate = tools._validate_categories
    first = True

    async def pause(categories):
        nonlocal first
        if first:
            first = False
            verified.set()
            await resume.wait()
        await validate(categories)

    monkeypatch.setattr(tools, "_validate_categories", pause)
    return verified, resume


@pytest.mark.parametrize("mode", ["alias", "hash", "transform"])
async def test_reused_owner_concurrent_updates_keep_their_own_raw_backup_and_cas(
    native, monkeypatch, mode
):
    engine, tools, owner, backup, writes, tasks = _reused_owner(native, monkeypatch)
    first_before = copy.deepcopy(owner.current[0])
    second_before = {
        "id": "other",
        "alias": "Other",
        "triggers": [],
        "actions": [{"delay": 3}],
    }
    owner.current.append(copy.deepcopy(second_before))
    verified, resume = _pause_first_validation(tools, monkeypatch)
    first = asyncio.create_task(
        engine.call(
            "ha_config_set_automation",
            _arguments(first_before, mode),
            CallContext("actor"),
        )
    )
    try:
        await asyncio.wait_for(verified.wait(), 5)
        second = await engine.call(
            "ha_config_set_automation", _arguments(second_before), CallContext("actor")
        )
    finally:
        resume.set()
        first_result = await first
        await asyncio.gather(*tasks)
    assert not second.is_error, second.data
    assert not first_result.is_error, first_result.data
    assert len(writes) == 2
    assert {row["id"]: row["actions"] for row in owner.current} == {
        "uid": [{"delay": 2}],
        "other": [{"delay": 2}],
    }
    assert [call.args[0]["current_config"] for call in backup.await_args_list] == [
        second_before,
        first_before,
    ]


@pytest.mark.parametrize(
    "overlap", [False, True], ids=["after-update", "during-update"]
)
async def test_reused_owner_create_does_not_inherit_an_update_lock_image(
    native, monkeypatch, overlap
):
    engine, tools, owner, backup, writes, tasks = _reused_owner(native, monkeypatch)
    before = copy.deepcopy(owner.current[0])
    monkeypatch.setattr(tools, "_wait_automation_entity", AsyncMock(return_value=None))
    verified, resume = _pause_first_validation(tools, monkeypatch)
    update = asyncio.create_task(
        engine.call(
            "ha_config_set_automation", _arguments(before), CallContext("actor")
        )
    )
    try:
        await asyncio.wait_for(verified.wait(), 5)
        if not overlap:
            resume.set()
            updated = await update
            assert not updated.is_error, updated.data
        created = await engine.call(
            "ha_config_set_automation",
            {
                "config": {"alias": "Fresh", "triggers": [], "actions": [{"delay": 4}]},
                "wait": False,
                "MandatoryBPS": False,
            },
            CallContext("actor"),
        )
    finally:
        resume.set()
        updated = await update
        await asyncio.gather(*tasks)
    assert not updated.is_error, updated.data
    assert not created.is_error, created.data
    assert len(writes) == 2 and created.data["operation"] == "created"
    assert {row["id"]: row["actions"] for row in owner.current} == {
        "uid": [{"delay": 2}],
        created.data["unique_id"]: [{"delay": 4}],
    }
    snapshots = {
        call.args[0]["backup_id"]: call.args[0]["current_config"]
        for call in backup.await_args_list
    }
    assert snapshots == {"uid": before, None: None}


async def test_reused_owner_stale_update_still_conflicts_before_backup_or_write(
    native, monkeypatch
):
    engine, tools, owner, backup, writes, tasks = _reused_owner(native, monkeypatch)
    before = copy.deepcopy(owner.current[0])
    verified, resume = _pause_first_validation(tools, monkeypatch)
    first = asyncio.create_task(
        engine.call(
            "ha_config_set_automation", _arguments(before), CallContext("actor")
        )
    )
    try:
        await asyncio.wait_for(verified.wait(), 5)
        second = await engine.call(
            "ha_config_set_automation", _arguments(before), CallContext("actor")
        )
    finally:
        resume.set()
        stale = await first
        await asyncio.gather(*tasks)
    assert not second.is_error, second.data
    assert stale.is_error and stale.data["error"]["code"] == "CONFIG_CONFLICT", (
        stale.data
    )
    assert len(writes) == 1 and backup.await_count == 1
    assert owner.current[0]["actions"] == [{"delay": 2}]


async def test_stored_view_hash_preserves_raw_transform_and_legacy_view(
    native, monkeypatch
):
    engine, _tools, owner, backup, writes, tasks = _reused_owner(native, monkeypatch)
    before = copy.deepcopy(owner.current[0])
    actor = CallContext("actor")
    from ha_mcp.runtime.config_reads import AutomationReadTools

    reader = AutomationReadTools(engine.backend, actor)
    compatible = await reader.ha_config_get_automation(identifier="uid")
    stored = await reader.ha_config_get_automation(
        entity_id="uid", config_format="stored"
    )
    assert stored["config"] == before
    assert "actions" in compatible["config"] and "action" not in compatible["config"]
    assert compatible["config_hash"] == compute_config_hash(compatible["config"])
    assert stored["config_hash"] == compute_config_hash(before)
    result = await engine.call(
        "ha_config_set_automation",
        {
            "entity_id": "uid",
            "config_hash": stored["config_hash"],
            "python_transform": "config['action'][0]['delay'] = 7",
            "wait": False,
            "MandatoryBPS": False,
        },
        actor,
    )
    await asyncio.gather(*tasks)
    assert not result.is_error, result.data
    assert len(writes) == 1
    assert backup.await_args.args[0]["current_config"] == before
    assert owner.current[0]["action"][0]["delay"] == 7
