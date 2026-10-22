"""Thread / OpenThread Border Router (OTBR) handler for ``ha_manage_radio``.

Thread is network-scoped, not per-device: operations target the Thread network
and its border router rather than an individual node. Border-router writes use
the ``otbr/*`` WebSocket API and take an OTBR ``extended_address`` — resolved
from ``otbr/info`` when the caller omits it, which covers the common
single-OTBR setup. Dataset management and router discovery use the
integration-wide ``thread/*`` API. When no OTBR is configured the read-only
``network_status`` degrades to an ``available: False`` payload; border-router
writes raise instead so an unconfigured integration is not a silent no-op.
"""

from __future__ import annotations

from typing import Any

from ha_mcp.runtime.radio_specs import THREAD

from .base import (
    integration_not_found,
    integration_required,
    ok,
    resolve_entry_id,
    ws_call,
)

SUPPORTED = THREAD


async def _resolve_extended_address(client: Any, args: dict[str, Any]) -> str | None:
    """Return the target OTBR extended address.

    Uses the caller-supplied value, else the first (typically only) border
    router reported by ``otbr/info``. Returns None when no OTBR is present.
    """
    addr = args.get("extended_address")
    if addr:
        return str(addr)
    info = await ws_call(client, "otbr/info")
    return next(iter(info or {}), None)


async def handle(client: Any, action: str, args: dict[str, Any]) -> dict[str, Any]:
    """Execute one Thread/OTBR action (validation/confirm applied by caller)."""
    if action == "network_status":
        entry_id = await resolve_entry_id(client, "otbr")
        if not entry_id:
            return integration_not_found("thread", "otbr")
        info = await ws_call(client, "otbr/info")
        return ok(
            "thread",
            "network_status",
            config_entry_id=entry_id,
            border_routers=info,
        )

    if action == "list_datasets":
        datasets = await ws_call(client, "thread/list_datasets")
        return ok("thread", "list_datasets", datasets=datasets)

    if action == "discover_routers":
        # Subscription command: it streams discovered routers as events. Kick
        # off the scan and report it started; consumers watch the event stream.
        await ws_call(client, "thread/discover_routers")
        return ok(
            "thread",
            "discover_routers",
            long_running=True,
            note=(
                "Router discovery started; results are delivered as an event "
                "stream, not in this response."
            ),
        )

    if action == "add_dataset":
        result = await ws_call(
            client,
            "thread/add_dataset_tlv",
            source=args.get("source"),
            tlv=args.get("tlv"),
        )
        return ok("thread", "add_dataset", result=result)

    if action in ("set_preferred_dataset", "delete_dataset"):
        result = await ws_call(
            client, f"thread/{action}", dataset_id=args.get("dataset_id")
        )
        return ok("thread", action, result=result)

    return await _handle_otbr(client, action, args)


async def _handle_otbr(
    client: Any, action: str, args: dict[str, Any]
) -> dict[str, Any]:
    """Execute a border-router write, requiring a configured OTBR."""
    extended_address = await _resolve_extended_address(client, args)
    if not extended_address:
        integration_required("thread", "otbr")
    ctx = {"extended_address": extended_address}

    if action == "create_network":
        result = await ws_call(
            client,
            "otbr/create_network",
            extended_address=extended_address,
            context=ctx,
        )
        return ok(
            "thread", "create_network", result=result, extended_address=extended_address
        )

    if action == "set_network":
        result = await ws_call(
            client,
            "otbr/set_network",
            extended_address=extended_address,
            dataset_id=args.get("dataset_id"),
            context=ctx,
        )
        return ok(
            "thread", "set_network", result=result, extended_address=extended_address
        )

    if action == "set_channel":
        result = await ws_call(
            client,
            "otbr/set_channel",
            extended_address=extended_address,
            channel=args.get("channel"),
            context=ctx,
        )
        return ok(
            "thread", "set_channel", result=result, extended_address=extended_address
        )

    # Unreachable: dispatcher validates action against SUPPORTED first.
    raise AssertionError(f"unhandled thread action: {action}")
