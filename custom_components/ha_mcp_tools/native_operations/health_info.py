"""Resolve Core health values without a subscription or cancelling shared futures."""

import asyncio
import json
from typing import Any

from homeassistant.core import HomeAssistant

from ha_mcp.tool_api import CallContext, UnsupportedOperation
from ha_mcp.utils.owned_executor import await_owned_io

from .registry_access import Authorize, authorize_actor


def _register_values(
    owner: Any,
    domain: str,
    snapshot: dict[str, Any],
    pending: dict[asyncio.Future[Any], list[tuple[str, str]]],
    owned: set[asyncio.Task[Any]],
) -> None:
    for key, value in snapshot["info"].items():
        candidate = value
        if asyncio.iscoroutine(candidate):
            candidate = asyncio.create_task(candidate)
            owned.add(candidate)
        if isinstance(candidate, asyncio.Future):
            pending.setdefault(candidate, []).append((domain, key))
            snapshot["info"][key] = {"type": "pending"}
        else:
            snapshot["info"][key] = owner._format_value(candidate)


def _future_value(owner: Any, future: asyncio.Future[Any]) -> Any:
    if future.cancelled():
        return {"type": "failed", "error": "cancelled"}
    if future.exception() is not None:
        return {"type": "failed", "error": "unknown"}
    return owner._format_value(future.result())


async def _settle_values(
    owner: Any,
    domains: dict[str, Any],
    pending: dict[asyncio.Future[Any], list[tuple[str, str]]],
    owned: set[asyncio.Task[Any]],
    deadline: float,
) -> bool:
    if not pending:
        return False
    done, unresolved = await asyncio.wait(
        pending, timeout=max(0, deadline - asyncio.get_running_loop().time())
    )
    for future in done:
        value = _future_value(owner, future)
        for domain, key in pending[future]:
            domains[domain]["info"][key] = value
    for future in unresolved:
        if future in owned:
            for domain, key in pending[future]:
                domains[domain]["info"][key] = {"type": "failed", "error": "timeout"}
    return bool(unresolved)


async def _collect(hass: HomeAssistant, owner: Any, timeout: float) -> dict[str, Any]:
    from homeassistant.helpers.json import json_bytes

    deadline = asyncio.get_running_loop().time() + timeout
    domains: dict[str, Any] = {}
    pending: dict[asyncio.Future[Any], list[tuple[str, str]]] = {}
    owned: set[asyncio.Task[Any]] = set()
    try:
        # Core owns lazy discovery, registration and its per-callback timeout.
        # The enclosing task is shielded so cancellation cannot propagate into
        # the platform loader's shared futures.
        async for domain, data in owner._registered_domain_data(hass):
            snapshot = {**data, "info": dict(data["info"])}
            domains[domain] = snapshot
            _register_values(owner, domain, snapshot, pending, owned)
        unresolved = await _settle_values(owner, domains, pending, owned, deadline)
        result: dict[str, Any] = {
            "success": True,
            "health_info": {"type": "initial", "data": domains},
            "component_count": len(domains),
            "message": f"Retrieved health info for {len(domains)} components",
        }
        if unresolved:
            result["warnings"] = [
                "Some system health values did not finish within the observation window. "
                "Any shared Core futures were left running rather than cancelled."
            ]
        detached: dict[str, Any] = json.loads(json_bytes(result))
        return detached
    finally:
        for task in owned:
            if not task.done():
                task.cancel()
        if owned:
            await await_owned_io(asyncio.gather(*owned, return_exceptions=True))


async def read_health_info(
    hass: HomeAssistant,
    actor: CallContext,
    authorize: Authorize,
    *,
    timeout: float = 10,
) -> dict[str, Any]:
    from homeassistant.components import system_health as owner

    await authorize_actor(actor, authorize)
    if owner.DATA_SYSTEM_HEALTH_PLATFORMS not in hass.data:
        raise UnsupportedOperation("Core system_health is not loaded")
    # Cancellation does not release newly-created probes before they drain.
    # Externally-created Future/Task values belong to Core, not this call.
    result = await await_owned_io(_collect(hass, owner, timeout))
    await authorize_actor(actor, authorize)
    return result
