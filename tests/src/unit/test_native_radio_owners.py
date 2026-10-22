"""Loaded radio-owner contracts; these fakes do not claim physical radio tests."""

import copy
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from .test_native_engine_backend import native as native
from .test_native_radios import call
from .test_native_radios import radios as radios


@pytest.fixture
def zwave(radios, monkeypatch):
    values = {
        "node-5": SimpleNamespace(
            property_=5,
            property_key=None,
            endpoint=0,
            configuration_value_type=SimpleNamespace(value="full"),
            value=12,
            metadata=SimpleNamespace(
                description="Future parameter",
                label="Mode",
                type="number",
                min=0,
                max=99,
                unit=None,
                writeable=True,
                readable=True,
                default=1,
                states={1: "On"},
            ),
        )
    }
    node = SimpleNamespace(
        get_configuration_values=lambda: values,
        async_get_raw_config_parameter_value=AsyncMock(return_value=23),
        async_refresh_info=AsyncMock(),
    )
    controller = SimpleNamespace(
        nodes={1: node}, async_begin_rebuilding_routes=AsyncMock(return_value=True)
    )
    driver = SimpleNamespace(controller=controller)
    entry = radios.loaded(
        "zwave_js", SimpleNamespace(client=SimpleNamespace(driver=driver))
    )
    row = SimpleNamespace(
        identifiers={("zwave_js", "home-1")}, config_entries={entry.entry_id}
    )
    registry = SimpleNamespace(async_get=lambda _identifier: row)
    radios.hass.data["device_registry"] = registry
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.zwave_js.helpers",
        SimpleNamespace(async_get_node_from_device_id=lambda *_: node),
    )
    for command in (
        "get_config_parameters",
        "get_raw_config_parameter",
        "refresh_node_info",
        "begin_rebuilding_routes",
    ):
        radios.command("zwave_js/" + command)
    return SimpleNamespace(
        radios=radios,
        node=node,
        values=values,
        controller=controller,
        driver=driver,
        entry=entry,
        registry=registry,
    )


async def test_zwave_parameter_reads_keep_cache_metadata_and_do_not_create_entities(
    zwave,
):
    cached = await call(
        zwave.radios,
        "zwave",
        "get_config_param",
        device_id="fixture",
        params={"property": 5},
    )
    assert not cached.is_error and cached.data["source"] == "cache", cached
    assert cached.data["parameter"]["value"] == 12
    assert cached.data["parameter"]["metadata"]["description"] == "Future parameter"
    zwave.node.async_get_raw_config_parameter_value.assert_not_awaited()
    refreshed = await call(
        zwave.radios,
        "zwave",
        "get_config_param",
        device_id="fixture",
        params={"property": 5, "refresh": True},
    )
    assert not refreshed.is_error and refreshed.data["parameter"]["value"] == 23, (
        refreshed
    )
    assert refreshed.data["metadata_source"] == "cache"
    zwave.node.async_get_raw_config_parameter_value.assert_awaited_once_with(5)
    assert "entity_registry" not in zwave.radios.hass.data


async def test_zwave_no_raw_value_never_falls_back_to_cache(zwave):
    zwave.node.async_get_raw_config_parameter_value.return_value = None
    result = await call(
        zwave.radios,
        "zwave",
        "get_config_param",
        device_id="fixture",
        params={"property": 5, "refresh": True},
    )
    assert (
        result.is_error and "no cached fallback" in result.data["error"]["message"]
    ), result
    assert zwave.node.async_get_raw_config_parameter_value.await_count == 1


async def test_zwave_node_and_network_maintenance_reach_distinct_core_owners(zwave):
    node = await call(zwave.radios, "zwave", "reinterview", device_id="fixture")
    network = await call(
        zwave.radios, "zwave", "rebuild_routes", params={"scope": "network"}
    )
    assert not node.is_error and not network.is_error
    zwave.node.async_refresh_info.assert_awaited_once()
    zwave.controller.async_begin_rebuilding_routes.assert_awaited_once()


async def test_smartstart_never_overwrites_a_placeholder_created_during_metadata_lookup(
    zwave, monkeypatch
):
    radios = zwave.radios
    registry = zwave.registry
    current = [None]
    registry.async_get_device_by_identifier = lambda *_: current[0]
    registry.async_get_or_create = Mock(
        side_effect=AssertionError("Registry write must not occur")
    )
    registry.async_update_device = Mock(
        side_effect=AssertionError("Registry write must not occur")
    )
    zwave.controller.own_node = None
    zwave.controller.async_provision_smart_start_node = AsyncMock()

    async def lookup(*_args):
        current[0] = SimpleNamespace(id="external-placeholder")
        return SimpleNamespace(manufacturer="Fixture", label="Device")

    zwave.driver.config_manager = SimpleNamespace(lookup_device=lookup)
    qr = SimpleNamespace(
        version="smart_start",
        additional_properties={},
        manufacturer_id=1,
        product_type=2,
        product_id=3,
        dsk="fixture",
        security_classes=[],
        requested_security_classes=[],
    )

    def validate(message):
        return {**message, "qr_provisioning_information": qr}

    radios.command("zwave_js/provision_smart_start_node", validate)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.zwave_js.api",
        SimpleNamespace(
            QRCodeVersion=SimpleNamespace(S2="s2"), ProvisioningEntry=SimpleNamespace
        ),
    )
    helpers = sys.modules["homeassistant.components.zwave_js.helpers"]
    monkeypatch.setattr(
        helpers, "get_device_id", lambda *_: ("zwave_js", "controller"), raising=False
    )
    result = await call(
        radios,
        "zwave",
        "add",
        params={
            "smart_start": True,
            "qr_provisioning_information": {},
            "device_name": "Fixture",
        },
    )
    assert result.is_error and result.status == "not_started", result
    registry.async_get_or_create.assert_not_called()
    zwave.controller.async_provision_smart_start_node.assert_not_awaited()


async def test_zha_group_creation_uses_core_normalized_members(radios, monkeypatch):
    normalized = object()
    group = SimpleNamespace(group_id=9)
    gateway = SimpleNamespace(
        application_controller=object(),
        async_create_zigpy_group=AsyncMock(return_value=group),
    )
    proxy = SimpleNamespace(
        gateway=gateway,
        group_proxies={9: SimpleNamespace(group_info={"id": 9, "future": "Core"})},
    )
    radios.loaded("zha", object())
    radios.hass.data["zha"] = SimpleNamespace(gateway_proxy=proxy)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.zha.const",
        SimpleNamespace(DATA_ZHA="zha"),
    )

    def validate(message):
        return {**message, "members": [normalized]}

    radios.command("zha/group/add", validate)
    result = await call(
        radios,
        "zigbee",
        "group_add",
        params={"group_name": "Fixture", "members": [{"ieee": "fixture"}]},
    )
    assert not result.is_error and result.data["group"]["future"] == "Core", result
    gateway.async_create_zigpy_group.assert_awaited_once_with(
        "Fixture", [normalized], None
    )


async def test_registered_service_preserves_actor_and_only_its_changed_states(radios):
    events = []
    registration = SimpleNamespace(schema=lambda data: {**data, "normalized": True})
    seen = []

    def listen(_event, callback):
        events.append(callback)
        return lambda: events.remove(callback)

    async def service(domain, name, data, **kwargs):
        seen.append((domain, name, copy.deepcopy(data), kwargs))
        for callback in events:
            callback(
                SimpleNamespace(
                    context=SimpleNamespace(user_id="other"),
                    data={
                        "new_state": SimpleNamespace(
                            as_dict=lambda: {"entity_id": "hidden"}
                        )
                    },
                )
            )
            callback(
                SimpleNamespace(
                    context=kwargs["context"],
                    data={
                        "new_state": SimpleNamespace(
                            as_dict=lambda: {
                                "entity_id": "light.fixture",
                                "state": "on",
                            }
                        )
                    },
                )
            )

    radios.hass.bus = SimpleNamespace(async_listen=listen)
    radios.hass.services = SimpleNamespace(
        has_service=lambda *_: True,
        async_services=lambda: {"zha": {"remove": registration}},
        async_call=service,
    )
    session = radios.access.RadioSession(radios.hass, radios.actor, radios.authorize)
    result = await session.service("zha", "remove", {"ieee": "fixture"})
    assert result == [{"entity_id": "light.fixture", "state": "on"}]
    assert seen[0][2]["normalized"] and seen[0][3]["context"].user_id == "operator"
    assert seen[0][3]["blocking"] and not events


async def test_matter_core_serialization_preserves_future_diagnostics_and_protects_own_fabric(
    radios, monkeypatch
):
    client = SimpleNamespace(
        node_diagnostics=AsyncMock(
            return_value=SimpleNamespace(
                data={
                    "active_fabric_index": 1,
                    "active_fabrics": [1, 2],
                    "ip_adresses": ["fd00::1"],
                    "future": {"keep": True},
                }
            )
        ),
        remove_matter_fabric=AsyncMock(),
    )
    entry = radios.loaded(
        "matter", SimpleNamespace(adapter=SimpleNamespace(matter_client=client))
    )
    node = SimpleNamespace(node_id=7)
    row = SimpleNamespace(
        identifiers={("matter", "fixture")}, config_entries={entry.entry_id}
    )
    radios.hass.data["device_registry"] = SimpleNamespace(async_get=lambda _: row)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.matter.helpers",
        SimpleNamespace(node_from_ha_device_id=lambda *_: node),
    )
    serializer = Mock(side_effect=lambda value: value.data)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.matter.api",
        SimpleNamespace(dataclass_to_dict=serializer),
    )
    for command in ("node_diagnostics", "remove_matter_fabric"):
        radios.command("matter/" + command)
    diagnostics = await call(radios, "matter", "diagnostics", device_id="fixture")
    assert not diagnostics.is_error and diagnostics.data["diagnostics"]["future"] == {
        "keep": True
    }, diagnostics
    assert (
        diagnostics.data["diagnostics"]["ip_addresses"] == ["fd00::1"]
        and serializer.call_count == 1
    )
    own = await call(
        radios,
        "matter",
        "remove_fabric",
        device_id="fixture",
        params={"fabric_index": 1},
        confirm=True,
    )
    assert own.is_error and own.status == "not_started", own
    client.remove_matter_fabric.assert_not_awaited()
    other = await call(
        radios,
        "matter",
        "remove_fabric",
        device_id="fixture",
        params={"fabric_index": 2},
        confirm=True,
    )
    assert not other.is_error, other
    client.remove_matter_fabric.assert_awaited_once_with(node_id=7, fabric_index=2)


def test_radio_signature_coercion_and_hints_are_shared_with_legacy(radios):
    import inspect

    from ha_mcp.runtime.radio_tools import RadioFacade
    from ha_mcp.tools.tools_radio import RadioTools

    assert inspect.signature(RadioTools.ha_manage_radio) == inspect.signature(
        RadioFacade.ha_manage_radio
    )
    definition = radios.engine.tools["ha_manage_radio"]
    assert (
        definition.annotations["destructiveHint"]
        and not definition.annotations["idempotentHint"]
    )
    assert not definition.annotations["openWorldHint"]
    assert definition.tags == frozenset(
        {"Radio Management", "Z-Wave", "Zigbee", "Matter", "Thread"}
    )
    validated = definition.validate(
        {"radio": "zigbee", "action": "permit_join", "params": '{"duration": 0}'}
    )
    assert validated["params"] == {"duration": 0} and validated["confirm"] is False
