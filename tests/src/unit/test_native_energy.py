"""Native Energy saves retain authority, Core normalization and owned callbacks."""

import asyncio
import copy
import importlib
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext
from ha_mcp.utils.config_hash import compute_config_hash

from .test_native_engine_backend import native as native


class CoreOwner:
    def __init__(self) -> None:
        self.data = {**self.default_preferences(), "future": {"nested": "keep"}}
        self.calls: list[dict[str, Any]] = []
        self.callback: Any = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False

    @staticmethod
    def default_preferences():
        return {
            "energy_sources": [],
            "device_consumption": [],
            "device_consumption_water": [],
        }

    @staticmethod
    def _process_energy_sources(sources):
        # Exercise Core's ability to retain input dictionaries and add fields;
        # actual installed source types and validators are covered by Container.
        return [
            {**row, "Core_generated": "normalized"} if row.get("normalize") else row
            for row in sources
        ]

    async def async_update(self, payload):
        self.calls.append(copy.deepcopy(payload))
        values = (self.data or self.default_preferences()).copy()
        values.update(payload)
        if "energy_sources" in payload:
            values["energy_sources"] = self._process_energy_sources(
                payload["energy_sources"]
            )
        self.data = values
        self.started.set()
        if self.block:
            await self.release.wait()
        if self.callback:
            self.callback(self)


@pytest.fixture
def energy(native, monkeypatch):
    owner = CoreOwner()
    hass = SimpleNamespace(data={"energy_manager": owner})
    registration = (object(), copy.deepcopy)
    hass.data["websocket_api"] = {"energy/save_prefs": registration}
    active = [True]

    async def authorize(_actor):
        if not active[0]:
            raise PermissionError("Actor revoked")

    async def get_manager(_hass):
        return hass.data["energy_manager"]

    async def validate(_hass):
        return SimpleNamespace(as_dict=dict)

    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.energy.data",
        SimpleNamespace(async_get_manager=get_manager),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.energy.validate",
        SimpleNamespace(async_validate=validate),
    )
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.energy"
    )
    binder = importlib.import_module(
        "custom_components.ha_mcp_tools.native_energy_tools"
    )
    backend = SimpleNamespace(authorize=authorize)
    manager = SimpleNamespace(hass=hass, backend=backend)
    engine = Engine(backend, [binder.energy_definition(manager)])
    actor = CallContext("actor")
    service = module.NativeEnergy(hass, actor, authorize=authorize)
    return SimpleNamespace(
        owner=owner,
        hass=hass,
        module=module,
        engine=engine,
        active=active,
        service=service,
        actor=actor,
    )


async def call(energy, arguments):
    return await energy.engine.call("ha_manage_energy_prefs", arguments, energy.actor)


async def test_native_save_uses_core_normalization_without_losing_other_preferences(
    energy,
):
    result = await call(
        energy,
        {
            "mode": "set",
            "config": {"energy_sources": [{"normalize": True}]},
            "config_hash": compute_config_hash(energy.owner.data),
        },
    )
    assert not result.is_error, result.data
    assert result.data["config"] == energy.owner.data
    assert result.data["config"]["energy_sources"][0]["Core_generated"] == "normalized"
    assert result.data["config"]["future"] == {"nested": "keep"}
    assert energy.owner.calls == [{"energy_sources": [{"normalize": True}]}]


async def test_preview_validates_the_proposal_without_initializing_preferences(energy):
    energy.owner.data = None
    result = await call(
        energy, {"mode": "set", "config": {"device_consumption": []}, "dry_run": True}
    )
    assert not result.is_error and result.data["proposal_validation"]["valid"] is True
    assert energy.owner.data is None and energy.owner.calls == []
    read = await call(energy, {"mode": "get"})
    assert read.data["config"] == energy.owner.default_preferences()
    assert "never been configured" in read.data["note"]


@pytest.mark.parametrize("race", ["authority", "policy", "owner", "schema", "values"])
async def test_queued_native_save_checks_admission_and_identity_inside_worker(
    energy, monkeypatch, race
):
    original = energy.module.await_owned_io

    async def queued(awaitable):
        if (
            getattr(awaitable, "cr_code", None)
            and awaitable.cr_code.co_name == "_commit"
        ):
            if race == "authority":
                energy.active[0] = False
            elif race == "policy":
                energy.engine.read_only = True
            elif race == "owner":
                energy.hass.data["energy_manager"] = CoreOwner()
            elif race == "schema":
                energy.hass.data["websocket_api"]["energy/save_prefs"] = (
                    None,
                    lambda payload: payload,
                )
            else:
                energy.owner.data["future"]["nested"] = "external"
        return await original(awaitable)

    monkeypatch.setattr(energy.module, "await_owned_io", queued)
    result = await call(
        energy,
        {
            "mode": "set",
            "config": {"device_consumption": []},
            "config_hash": compute_config_hash(energy.owner.data),
        },
    )
    assert result.is_error and result.status == "not_started", result.data
    assert energy.owner.calls == []


async def test_listener_cannot_mutate_the_detached_energy_confirmation_image(energy):
    def mutate(owner):
        owner.data["energy_sources"][0]["stat_energy_from"] = "sensor.external"

    energy.owner.callback = mutate
    result = await call(
        energy,
        {
            "mode": "add_source",
            "source": {"type": "solar", "stat_energy_from": "sensor.expected"},
        },
    )
    assert result.is_error and result.status == "outcome_unknown", result.data
    assert len(energy.owner.calls) == 1


@pytest.mark.parametrize("cancelled", [False, True])
async def test_core_error_after_energy_effect_is_unknown_without_retry(
    energy, cancelled
):
    def fail(_owner):
        if cancelled:
            raise asyncio.CancelledError("Core listener cancelled after saving")
        raise RuntimeError("Core listener failed after saving")

    energy.owner.callback = fail
    result = await call(
        energy, {"mode": "add_device", "stat_consumption": "sensor.device"}
    )
    assert result.is_error and result.status == "outcome_unknown"
    assert len(energy.owner.calls) == 1
    assert energy.owner.data["device_consumption"] == [
        {"stat_consumption": "sensor.device"}
    ]


async def test_cancelled_energy_waiter_keeps_the_mutex_until_core_callbacks_drain(
    energy,
):
    energy.owner.block = True
    first = asyncio.create_task(
        energy.service.add_device(
            stat_consumption="sensor.first",
            name=None,
            included_in_stat=None,
            water=False,
            dry_run=False,
        )
    )
    await energy.owner.started.wait()
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    second = asyncio.create_task(
        energy.service.add_device(
            stat_consumption="sensor.second",
            name=None,
            included_in_stat=None,
            water=False,
            dry_run=False,
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    kept_lock = not second.done() and len(energy.owner.calls) == 1
    energy.owner.release.set()
    with pytest.raises(asyncio.CancelledError):
        _ = await first
    result = await second
    assert kept_lock and result["success"]
    assert energy.owner.data["device_consumption"] == [
        {"stat_consumption": "sensor.first"},
        {"stat_consumption": "sensor.second"},
    ]


async def test_missing_energy_integration_refuses_without_materializing_an_owner(
    energy, monkeypatch
):
    energy.hass.data["websocket_api"].clear()
    getter = AsyncMock(side_effect=AssertionError("No cold Core owner expected"))
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.energy.data",
        SimpleNamespace(async_get_manager=getter),
    )
    result = await call(energy, {"mode": "get"})
    assert result.is_error and result.data["error"]["code"] == "SERVICE_UNAVAILABLE"
    getter.assert_not_awaited()


@pytest.mark.parametrize("race", ["authority", "values"])
async def test_post_save_validation_race_preserves_completed_write_without_disclosing_stale_result(
    energy, monkeypatch, race
):
    async def validate(_hass):
        await asyncio.sleep(0)
        if race == "authority":
            energy.active[0] = False
        else:
            energy.owner.data["future"]["nested"] = "newer Core preferences"
        return SimpleNamespace(as_dict=dict)

    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.energy.validate",
        SimpleNamespace(async_validate=validate),
    )
    result = await call(
        energy, {"mode": "add_device", "stat_consumption": "sensor.device"}
    )
    assert result.is_error and result.status == "confirmed", result.data
    assert result.data["write_completed"] is True
    assert result.data["operation_outcome"] == "confirmed"
    assert not {"config", "config_hash", "post_save_validation"} & result.data.keys()
    assert len(energy.owner.calls) == 1


@pytest.mark.parametrize("loading", [False, True])
async def test_read_only_energy_does_not_initiate_or_cancel_shared_core_storage_loading(
    energy, monkeypatch, loading
):
    getter = AsyncMock(side_effect=AssertionError("Core owns initialization"))
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.energy.data",
        SimpleNamespace(async_get_manager=getter),
    )
    pending = asyncio.get_running_loop().create_future()
    if loading:
        energy.hass.data["energy_manager"] = pending
    else:
        energy.hass.data.pop("energy_manager")
    energy.engine.read_only = True
    result = await call(energy, {"mode": "get"})
    assert result.is_error and result.data["error"]["code"] == "SERVICE_UNAVAILABLE", (
        result.data
    )
    assert result.status == "not_started"
    getter.assert_not_awaited()
    assert not pending.done()
    pending.cancel()
