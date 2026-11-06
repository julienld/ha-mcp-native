"""The shared radio tool signature and safety gates, without remote imports."""

from typing import Annotated, Any, Literal

from pydantic import Field

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tools.coercion import JSON_STRING_COERCION

from .errors import raise_failure
from .radio_specs import SUPPORTED
from .radio_values import confirm_required, require


class RadioFacade:
    def __init__(self, radios: Any) -> None:
        self.radios = radios

    async def ha_manage_radio(
        self,
        radio: Annotated[
            Literal["zwave", "zigbee", "matter", "thread"],
            Field(description="Which radio to manage."),
        ],
        action: Annotated[
            str,
            Field(
                description=(
                    "Operation to perform. Actions vary per radio; an unknown "
                    "action returns the supported list for that radio. Common: "
                    "'diagnostics', 'network_status', 'ping', 'add'/'commission', "
                    "'remove_device', 'reinterview'/'reconfigure', 'firmware_update'."
                )
            ),
        ],
        device_id: Annotated[
            str | None,
            Field(
                description="Target device (node) for node-scoped actions.",
                default=None,
            ),
        ] = None,
        entity_id: Annotated[
            str | None,
            Field(
                description="Resolve the device from this entity for node-scoped actions.",
                default=None,
            ),
        ] = None,
        params: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Action-specific parameters (e.g. code, pin, channel, property, value). "
                    "Z-Wave get_config_param: property (parameter number), endpoint "
                    "(default 0), property_key (partial-parameter bit mask), refresh "
                    "(default False; full root parameters only)."
                ),
                default=None,
            ),
        ] = None,
        confirm: Annotated[
            bool,
            Field(
                description=(
                    "Required (True) to run destructive actions (e.g. "
                    "remove_device, network restore, change_channel, "
                    "hard_reset, remove_fabric)."
                ),
                default=False,
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Manage Home Assistant radios — Z-Wave, Zigbee, Matter, and Thread.

        For node diagnostics and network summaries prefer ha_get_device /
        ha_get_system_health, which mirror 'diagnostics' and 'network_status'.
        This tool also exposes active 'ping' probes and parameter reads.
        Z-Wave 'get_config_params' lists cached configuration values/metadata;
        'get_config_param' reads one from the cache or explicitly from the device.
        Neither enables or creates entities. Write actions perform
        inclusion/commissioning, removal, healing,
        reconfiguration, firmware updates and credential provisioning.

        Caveats: long_running=true identifies work whose overall completion may
        occur after the response (inclusion, route rebuilding, firmware). Core
        calls that wait for a receipt are awaited; this flag is not a guarantee
        that the call returns immediately. On the native component,
        Thread discover_routers returns observations from a three-second window,
        sets complete=false and stops its own listener before returning; it does
        not expose a persistent event stream. ZHA network_status reads current
        settings without initiating a topology scan. Interactive Z-Wave S2 secure inclusion (read-the-
        PIN pairing) is not scriptable — use SmartStart/QR provisioning here or
        the HA UI. HA administrator access is required for parameter reads.
        """
        spec = SUPPORTED[radio].get(action)
        if spec is None:
            supported = sorted(SUPPORTED[radio])
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Unknown action '{action}' for radio '{radio}'",
                    context={
                        "radio": radio,
                        "action": action,
                        "supported": supported,
                    },
                    # Surface each action's one-line summary so the caller
                    # can pick the right one without another round-trip.
                    suggestions=[
                        f"{name}: {SUPPORTED[radio][name].summary}"
                        for name in supported
                    ],
                )
            )

        args: dict[str, Any] = dict(params or {})
        if device_id:
            args.setdefault("device_id", device_id)
        if entity_id and not args.get("device_id"):
            args["device_id"] = await self.radios.resolve_entity_device(entity_id)

        require(args, spec, radio, action)
        if spec.destructive and not confirm:
            confirm_required(radio, action)

        result: dict[str, Any] = await self.radios.handle(radio, action, args)
        # ActionSpec is the single source of truth for long-running; fill it
        # in even on branches that did not set it (e.g. force-remove paths).
        if spec.long_running:
            result.setdefault("long_running", True)
        return result
