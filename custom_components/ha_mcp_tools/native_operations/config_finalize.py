"""Core registry confirmation and category updates after config persistence."""

import asyncio
import copy
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ha_mcp.tool_api import UnsupportedOperation
from ha_mcp.utils.python_sandbox import safe_execute

from .entity_waiters import wait_automation_entity, wait_registered, wait_state


async def resolve_after_write(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> str | None:
    from homeassistant.helpers import entity_registry as er

    domain = arguments["domain"]
    if domain == "automation":
        return await wait_automation_entity(hass, arguments)
    if domain not in {"scene", "script"}:
        raise UnsupportedOperation("config.entity_id.after_write.domain")
    key = arguments["storage_key"].removeprefix(f"{domain}.")
    fallback = arguments.get("fallback_entity_id") or f"{domain}.{key}"
    timeout = arguments.get("timeout")
    budget = 5.0 if timeout is None else max(0.0, min(5.0, float(timeout)))
    deadline = asyncio.get_running_loop().time() + budget
    platform = "homeassistant" if domain == "scene" else domain
    while True:
        entity_id = er.async_get(hass).async_get_entity_id(domain, platform, key)
        if entity_id is not None:
            return str(entity_id)
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return str(fallback)
        await asyncio.sleep(min(0.2, remaining))


async def apply_category(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> dict[str, Any]:
    from homeassistant.helpers import category_registry as cr
    from homeassistant.helpers import entity_registry as er

    scope = arguments["scope"]
    if scope not in {"automation", "script", "scene"}:
        raise UnsupportedOperation("entities.category.write.scope")
    category, entity_id = arguments["category"], arguments["entity_id"]
    kind = arguments["entity_type"].capitalize()
    if (
        category
        and cr.async_get(hass).async_get_category(scope=scope, category_id=category)
        is None
    ):
        return {
            "warnings": [
                f"{kind} saved but category {category!r} no longer exists in the {scope} registry — not applied."
            ]
        }
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is None:
        return {
            "warnings": [f"{kind} saved but failed to set category: Entity not found"]
        }
    categories = dict(entry.categories)
    categories.pop(scope, None)
    if category:
        categories[scope] = category
    try:
        registry.async_update_entity(entity_id, categories=categories)
    except (HomeAssistantError, ValueError) as error:
        return {"warnings": [f"{kind} saved but failed to set category: {error}"]}
    return {"category": category or None}


DIRECT_OPERATIONS = {
    "config.entity_id.after_write": (resolve_after_write, False),
    "entities.registration.wait": (wait_registered, False),
    "entities.category.write": (apply_category, True),
}


async def transform_config(hass: HomeAssistant, arguments: dict[str, Any]) -> Any:
    """Pure transforms run in Core's executor, keeping its loop responsive."""
    return await hass.async_add_executor_job(
        safe_execute, arguments["expression"], copy.deepcopy(arguments["config"])
    )


DIRECT_OPERATIONS["config.transform"] = (transform_config, False)


async def automation_allocate_id(hass: HomeAssistant, arguments: dict[str, Any]) -> str:
    from uuid import uuid4

    return uuid4().hex


async def automation_runtime_target(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> str | None:
    identifier = arguments["identifier"]
    return next(
        (
            state.entity_id
            for state in hass.states.async_all()
            if state.entity_id.startswith("automation.")
            and (
                state.entity_id == identifier
                or state.attributes.get("id") == identifier
            )
        ),
        None,
    )


DIRECT_OPERATIONS.update(
    {
        "config.automation.allocate_id": (automation_allocate_id, False),
        "config.automation.runtime_target": (automation_runtime_target, False),
        "entities.state.wait": (wait_state, False),
    }
)
