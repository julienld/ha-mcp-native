"""Radio effects use installed owners, frozen targets and call-owned workers."""

import asyncio
import copy
import importlib
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.runtime.radio_specs import SUPPORTED
from ha_mcp.tool_api import CallContext

from .test_component_ws_search import _REAL_VOL
from .test_native_engine_backend import native as native


@pytest.fixture
def radios(native, monkeypatch):
    loaded_state = object()
    entries = {}
    state = SimpleNamespace(active=True, hook=None)
    hass = SimpleNamespace(
        config=SimpleNamespace(components=set()),
        data={"websocket_api": {}},
        config_entries=SimpleNamespace(
            async_loaded_entries=lambda domain: [
                row for row in entries.values() if row.domain == domain
            ],
            async_get_entry=entries.get,
        ),
    )

    async def authorize(actor):
        if actor.credential:
            await actor.credential()
        if state.hook:
            state.hook()
        if not state.active:
            raise PermissionError("Actor revoked")

    access = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.radio_access"
    )
    registry_access = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.registry_access"
    )
    monkeypatch.setattr(
        registry_access, "json_bytes", lambda value: json.dumps(value).encode()
    )
    monkeypatch.setattr(
        access, "ConfigEntryState", SimpleNamespace(LOADED=loaded_state)
    )
    monkeypatch.setattr(access, "Context", SimpleNamespace)
    monkeypatch.setattr(access, "callback", lambda function: function)
    binder = importlib.import_module(
        "custom_components.ha_mcp_tools.native_radio_tools"
    )
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.radio_manager"
    )
    backend = SimpleNamespace(authorize=authorize)
    engine = Engine(
        backend, [binder.radio_definition(SimpleNamespace(hass=hass, backend=backend))]
    )
    actor = CallContext("operator")

    def loaded(domain, runtime):
        entry = SimpleNamespace(
            entry_id=domain + "_entry",
            domain=domain,
            state=loaded_state,
            runtime_data=runtime,
            data={"counter": 1},
            options={},
        )
        entries[entry.entry_id] = entry
        hass.config.components.add(domain)
        return entry

    def command(name, validator=copy.deepcopy):
        hass.data["websocket_api"][name] = (object(), validator)

    return SimpleNamespace(
        hass=hass,
        entries=entries,
        state=state,
        loaded=loaded,
        command=command,
        engine=engine,
        actor=actor,
        authorize=authorize,
        access=access,
        module=module,
    )


async def call(radios, radio, action, **kwargs):
    return await radios.engine.call(
        "ha_manage_radio", {"radio": radio, "action": action, **kwargs}, radios.actor
    )


@pytest.mark.parametrize(
    "radio,action",
    [(radio, action) for radio, actions in SUPPORTED.items() for action in actions],
)
async def test_absent_hardware_never_imports_optional_radio_integrations(
    radios, monkeypatch, radio, action
):
    import builtins

    original = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith(
            (
                "homeassistant.components.zha",
                "homeassistant.components.zwave_js",
                "homeassistant.components.matter",
                "homeassistant.components.thread",
                "homeassistant.components.otbr",
            )
        ):
            raise AssertionError(
                "Unavailable integration must not be imported: " + name
            )
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    params = dict.fromkeys(SUPPORTED[radio][action].required, "fixture")
    result = await call(radios, radio, action, params=params, confirm=True)
    if action == "network_status":
        assert not result.is_error and result.data["available"] is False, result
    else:
        assert result.is_error and result.status == "not_started", result
        assert result.data["error"]["code"] == "RADIO_UNAVAILABLE", result


@pytest.mark.parametrize(
    "radio,action",
    [
        (radio, action)
        for radio, specs in SUPPORTED.items()
        for action, spec in specs.items()
        if spec.destructive
    ],
)
async def test_destructive_radio_action_needs_confirmation_before_owner_resolution(
    radios, radio, action
):
    params = dict.fromkeys(SUPPORTED[radio][action].required, "fixture")
    result = await call(radios, radio, action, params=params)
    assert result.is_error and "confirm=True" in result.data["error"]["message"], result


@pytest.fixture
def matter(radios):
    client = SimpleNamespace(
        commission_with_code=AsyncMock(return_value=None),
        commission_on_network=AsyncMock(return_value=None),
        set_thread_operational_dataset=AsyncMock(return_value=None),
        set_wifi_credentials=AsyncMock(return_value=None),
    )
    entry = radios.loaded(
        "matter", SimpleNamespace(adapter=SimpleNamespace(matter_client=client))
    )
    for action in (
        "commission",
        "commission_on_network",
        "set_thread",
        "set_wifi_credentials",
    ):
        radios.command("matter/" + action)
    return SimpleNamespace(radios=radios, client=client, entry=entry)


async def test_matter_uses_the_registered_core_schema_and_protocol_owner(matter):
    seen = []

    def validate(message):
        seen.append(copy.deepcopy(message))
        return {**message, "pin": int(message["pin"])}

    matter.radios.command("matter/commission_on_network", validate)
    result = await call(
        matter.radios,
        "matter",
        "commission_on_network",
        params={"pin": "123", "ip_addr": "fd00::1"},
    )
    assert not result.is_error, result
    assert seen[0]["pin"] == "123"
    matter.client.commission_on_network.assert_awaited_once_with(123, ip_addr="fd00::1")


@pytest.mark.parametrize(
    "race",
    ["actor", "credential", "policy", "entry", "client", "typed_before", "validator"],
)
async def test_radio_worker_revalidates_live_authority_and_pinned_targets(
    matter, monkeypatch, race
):
    radios = matter.radios
    original = radios.access.RadioSession.effect

    async def revoked():
        raise PermissionError("expired token")

    async def effect(self, operation):
        if race == "actor":
            radios.state.active = False
        elif race == "credential":
            self.actor = CallContext("operator", credential=revoked)
        elif race == "policy":
            radios.engine.read_only = True
        elif race == "entry":
            radios.entries[matter.entry.entry_id] = copy.copy(matter.entry)
        elif race == "client":
            matter.entry.runtime_data.adapter.matter_client = object()
        elif race == "validator":
            radios.command("matter/commission")
        else:
            matter.entry.data["counter"] = True
        return await original(self, operation)

    monkeypatch.setattr(radios.access.RadioSession, "effect", effect)
    result = await call(radios, "matter", "commission", params={"code": "fixture"})
    assert result.is_error and result.status == "not_started", result
    matter.client.commission_with_code.assert_not_awaited()


@pytest.mark.parametrize(
    "error", [RuntimeError("after packet"), asyncio.CancelledError()]
)
async def test_radio_failure_after_dispatch_is_unknown_without_replay(matter, error):
    matter.client.commission_with_code.side_effect = error
    result = await call(
        matter.radios, "matter", "commission", params={"code": "fixture"}
    )
    assert result.is_error and result.status == "outcome_unknown", result
    assert matter.client.commission_with_code.await_count == 1


async def test_double_cancel_keeps_radio_mutex_until_owned_driver_call_drains(matter):
    radios = matter.radios
    started, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        started.set()
        await release.wait()

    matter.client.commission_with_code.side_effect = blocked

    async def write(code):
        service = radios.module.NativeRadios(
            radios.hass, radios.actor, radios.authorize
        )
        return await service.handle("matter", "commission", {"code": code})

    first = asyncio.create_task(write("first"))
    await started.wait()
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    second = asyncio.create_task(write("second"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    kept = (
        not first.done()
        and not second.done()
        and matter.client.commission_with_code.await_count == 1
    )
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    result = await second
    assert (
        kept
        and result["success"]
        and matter.client.commission_with_code.await_count == 2
    )


async def test_invalid_core_schema_has_no_driver_effect(matter):
    def invalid(_message):
        raise _REAL_VOL.Invalid("Core rejects the proposed credentials")

    matter.radios.command("matter/set_wifi_credentials", invalid)
    result = await call(
        matter.radios,
        "matter",
        "set_wifi_credentials",
        params={"network_name": "fixture", "password": "fixture"},
    )
    assert result.is_error and result.status == "not_started"
    matter.client.set_wifi_credentials.assert_not_awaited()


async def test_zha_network_status_never_starts_a_topology_scan(radios, monkeypatch):
    topology = SimpleNamespace(
        scan=AsyncMock(side_effect=AssertionError("Unexpected scan"))
    )
    app = SimpleNamespace(config={"device": {"path": "fixture"}}, topology=topology)
    gateway = SimpleNamespace(application_controller=app)
    proxy = SimpleNamespace(gateway=gateway)
    radios.loaded("zha", object())
    radios.hass.data["zha"] = SimpleNamespace(gateway_proxy=proxy)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.zha.const",
        SimpleNamespace(DATA_ZHA="zha"),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.zha.api",
        SimpleNamespace(
            CONF_DEVICE="device",
            async_get_radio_type=lambda *_: SimpleNamespace(name="fixture"),
            async_get_active_network_settings=lambda *_: SimpleNamespace(
                as_dict=lambda: {"channel": 20}
            ),
        ),
    )
    radios.command("zha/network/settings")
    radios.engine.read_only = True
    result = await call(radios, "zigbee", "network_status")
    assert (
        not result.is_error and result.data["network"]["settings"]["channel"] == 20
    ), result
    topology.scan.assert_not_awaited()


async def test_bounded_thread_discovery_returns_observations_and_stops_its_listener(
    radios, monkeypatch
):
    from dataclasses import dataclass

    radios.hass.config.components.add("thread")
    radios.command("thread/discover_routers")
    seen = []

    @dataclass
    class Router:
        network_name: str

    class Discovery:
        def __init__(self, hass, found, removed):
            self.found = found
            self.removed = removed
            seen.append(self)
            self.stopped = False

        async def async_start(self):
            self.found("router-a", Router("Fixture"))
            self.found("router-b", Router("Removed"))
            self.removed("router-b")

        async def async_stop(self):
            self.stopped = True

    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.thread.discovery",
        SimpleNamespace(ThreadRouterDiscovery=Discovery),
    )
    result = await call(radios, "thread", "discover_routers")
    assert not result.is_error, result
    assert result.data["routers"] == {"router-a": {"network_name": "Fixture"}}
    assert (
        not result.data["long_running"]
        and not result.data["complete"]
        and seen[0].stopped
    )
    seen[0].found("late", Router("Must not leak"))
    assert "late" not in result.data["routers"]


async def test_thread_discovery_cleans_up_after_core_start_failure(radios, monkeypatch):
    radios.hass.config.components.add("thread")
    radios.command("thread/discover_routers")
    stopped = []

    class Discovery:
        def __init__(self, *_args):
            pass

        async def async_start(self):
            raise RuntimeError("listener attached, then failed")

        async def async_stop(self):
            stopped.append(True)

    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.thread.discovery",
        SimpleNamespace(ThreadRouterDiscovery=Discovery),
    )
    result = await call(radios, "thread", "discover_routers")
    assert result.is_error and result.status == "outcome_unknown" and stopped == [True]


async def test_thread_dataset_typed_before_image_is_checked_after_authorization(
    radios, monkeypatch
):
    class Dataset:
        def __init__(self):
            self.value = 1

        def to_json(self):
            return {"value": self.value}

    dataset = Dataset()
    owner = SimpleNamespace(datasets={"dataset": dataset}, preferred_dataset=None)
    radios.hass.config.components.add("thread")
    radios.hass.data["thread.datasets"] = owner
    radios.command("thread/set_preferred_dataset")
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.thread.dataset_store",
        SimpleNamespace(
            DATA_STORE="thread.datasets", async_get_store=AsyncMock(return_value=owner)
        ),
    )
    original = radios.access.RadioSession.effect

    async def effect(self, operation):
        dataset.value = True
        return await original(self, operation)

    monkeypatch.setattr(radios.access.RadioSession, "effect", effect)
    result = await call(
        radios, "thread", "set_preferred_dataset", params={"dataset_id": "dataset"}
    )
    assert result.is_error and result.status == "not_started", result
    assert owner.preferred_dataset is None


async def test_otbr_revocation_after_disable_never_sends_dataset_or_reenable(
    radios, monkeypatch
):
    owner = SimpleNamespace(
        url="fixture",
        get_extended_address=AsyncMock(return_value=b"abc"),
        set_enabled=AsyncMock(),
        set_active_dataset_tlvs=AsyncMock(),
    )
    radios.loaded("otbr", owner)
    radios.hass.config.components.add("thread")
    row = SimpleNamespace(tlv="aabb", to_json=lambda: {"tlv": "aabb"})
    store = SimpleNamespace(async_get=lambda _identifier: row)
    radios.hass.data["thread.datasets"] = store
    radios.command("otbr/set_network")
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.thread.dataset_store",
        SimpleNamespace(
            DATA_STORE="thread.datasets", async_get_store=AsyncMock(return_value=store)
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.otbr.websocket_api",
        SimpleNamespace(
            tlv_parser=SimpleNamespace(parse_tlv=lambda _: {}),
            MeshcopTLVType=SimpleNamespace(CHANNEL="channel"),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.otbr.util",
        SimpleNamespace(
            get_allowed_channel=AsyncMock(return_value=None), update_issues=AsyncMock()
        ),
    )

    async def disable(_value):
        radios.state.active = False

    owner.set_enabled.side_effect = disable
    result = await call(
        radios, "thread", "set_network", params={"dataset_id": "fixture"}, confirm=True
    )
    assert result.is_error and result.status == "outcome_unknown", result
    owner.set_enabled.assert_awaited_once_with(False)
    owner.set_active_dataset_tlvs.assert_not_awaited()
