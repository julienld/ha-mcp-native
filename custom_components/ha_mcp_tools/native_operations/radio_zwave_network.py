"""Z-Wave controller inclusion, provisioning and reset through real owners."""

import asyncio
from typing import Any

from ha_mcp.runtime.radio_values import ok
from ha_mcp.runtime.radio_zwave_values import validate_inclusion
from ha_mcp.tool_api import ToolFailure

from .radio_access import RadioSession


async def network_action(
    session: RadioSession,
    entry: Any,
    driver: Any,
    controller: Any,
    action: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    if action == "hard_reset":
        return await _reset(session, entry, driver)
    if action == "add":
        if args.get("smart_start"):
            return await _smart_start(session, entry, driver, controller, args)
        validate_inclusion(args)
        from homeassistant.components.zwave_js.api import (
            INCLUSION_STRATEGY_NOT_SMART_START,
            InclusionState,
        )

        payload = session.validate(
            "zwave_js/add_node",
            {
                "entry_id": entry.entry_id,
                **{
                    key: args.get(key)
                    for key in (
                        "inclusion_strategy",
                        "qr_provisioning_information",
                        "qr_code_string",
                        "planned_provisioning_entry",
                        "dsk",
                    )
                },
            },
        )
        if controller.inclusion_state in (
            InclusionState.INCLUDING,
            InclusionState.BUSY,
        ):
            return ok(
                "zwave",
                action,
                mode="add_node",
                result=True,
                long_running=True,
                note="An inclusion is already running; no new inclusion was dispatched.",
            )
        result = await session.effect(
            lambda: controller.async_begin_inclusion(
                INCLUSION_STRATEGY_NOT_SMART_START[payload["inclusion_strategy"]],
                force_security=payload.get("force_security"),
                provisioning=payload.get("planned_provisioning_entry")
                or payload.get("qr_provisioning_information")
                or payload.get("qr_code_string"),
                dsk=payload.get("dsk"),
            )
        )
        return ok("zwave", action, mode="add_node", result=result, long_running=True)
    if action == "remove_device":
        payload = session.validate("zwave_js/remove_node", {"entry_id": entry.entry_id})
        result = await session.effect(
            lambda: controller.async_begin_exclusion(payload.get("strategy"))
        )
        return ok("zwave", action, mode="exclusion", result=result, long_running=True)
    session.validate("zwave_js/begin_rebuilding_routes", {"entry_id": entry.entry_id})
    result = await session.effect(controller.async_begin_rebuilding_routes)
    return ok("zwave", action, scope="network", result=result, long_running=True)


async def _smart_start(
    session: RadioSession,
    entry: Any,
    driver: Any,
    controller: Any,
    args: dict[str, Any],
) -> dict[str, Any]:
    from homeassistant.components.zwave_js.api import ProvisioningEntry, QRCodeVersion

    payload = session.validate(
        "zwave_js/provision_smart_start_node",
        {
            "entry_id": entry.entry_id,
            **{
                key: args.get(key)
                for key in ("qr_provisioning_information", "device_name", "area_id")
            },
        },
    )
    qr = payload["qr_provisioning_information"]
    if qr.version == QRCodeVersion.S2:
        raise ToolFailure(
            "VALIDATION_INVALID_PARAMETER",
            "S2 QR codes cannot be provisioned through SmartStart",
            status="not_started",
        )
    additional = dict(qr.additional_properties or {})
    device_id = None
    if payload.get("device_name"):
        device_id = await _provision_device(session, entry, driver, qr, payload)
        additional["device_id"] = device_id
    planned = ProvisioningEntry(
        dsk=qr.dsk,
        security_classes=qr.security_classes,
        requested_security_classes=qr.requested_security_classes,
        protocol=payload.get("protocol"),
        additional_properties=additional,
    )
    await session.effect(lambda: controller.async_provision_smart_start_node(planned))
    return ok("zwave", "add", mode="smart_start", result=device_id, long_running=True)


async def _provision_device(
    session: RadioSession, entry: Any, driver: Any, qr: Any, payload: dict[str, Any]
) -> str:
    from homeassistant.components.zwave_js.helpers import get_device_id
    from homeassistant.helpers import device_registry as dr

    registry = session.registry("device")
    identifier = ("zwave_js", f"provision_{qr.dsk}")
    # Pin the exact placeholder (including absence) before the metadata await.
    # Advance that expectation only to the entry returned by our own mutation.
    expected = [registry.async_get_device_by_identifier(identifier, entry.entry_id)]
    session.target_pins.append(
        lambda: (
            registry.async_get_device_by_identifier(identifier, entry.entry_id)
            is expected[0]
        )
    )
    manager = session.retain(lambda: driver.config_manager, driver.config_manager)
    info = await session.read(
        lambda: manager.lookup_device(
            qr.manufacturer_id, qr.product_type, qr.product_id
        )
    )
    via = (
        dr.async_get_device_id_by_identifier(
            session.hass,
            get_device_id(driver, driver.controller.own_node),
            config_entry_id=entry.entry_id,
        )
        if driver.controller.own_node
        else None
    )

    def create() -> str:
        device = registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={identifier},
            name=payload["device_name"],
            manufacturer=info.manufacturer if info else None,
            model=info.label if info else None,
            via_device_id=via,
        )
        expected[0] = registry.async_update_device(
            device.id,
            area_id=payload.get("area_id"),
            name_by_user=payload["device_name"],
        )
        return str(device.id)

    return str(await session.sync_effect(create))


async def _reset(session: RadioSession, entry: Any, driver: Any) -> dict[str, Any]:
    from homeassistant.components.zwave_js.helpers import (
        DRIVER_READY_EVENT_TIMEOUT,
        async_get_version_info,
    )

    session.validate("zwave_js/hard_reset_controller", {"entry_id": entry.entry_id})
    ready = asyncio.Event()
    unsubscribe = driver.once("driver ready", lambda _event: ready.set())
    try:
        await session.effect(driver.async_hard_reset)
        try:
            await asyncio.wait_for(ready.wait(), timeout=DRIVER_READY_EVENT_TIMEOUT)
        except TimeoutError:
            # The reset was already issued; no replay or speculative rebase.
            raise RuntimeError("Controller did not become ready after reset") from None
        info = await session.read(
            lambda: async_get_version_info(session.hass, entry.data["url"])
        )
        await session.sync_effect(
            lambda: session.hass.config_entries.async_update_entry(
                entry, unique_id=str(info.home_id)
            )
        )
        await session.sync_effect(
            lambda: session.hass.config_entries.async_schedule_reload(entry.entry_id)
        )
    finally:
        unsubscribe()
    return ok(
        "zwave",
        "hard_reset",
        result=None,
        long_running=True,
        note="Core accepted the reset and scheduled its entry reload; use network_status after reload for the new controller identity.",
    )
