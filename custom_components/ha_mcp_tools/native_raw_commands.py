"""Bounded compatibility names calling Core owners, never protocol handlers."""

from __future__ import annotations

import json
from contextlib import suppress
from importlib import import_module
from itertools import chain
from typing import Any

from homeassistant.helpers.json import json_bytes

from ha_mcp.runtime.mutation_guard import current_admission
from ha_mcp.runtime.websocket_policy import validate_ws_request
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation
from ha_mcp.utils.owned_executor import await_owned_io

from .native_operations.command_validation import _validated_payload

_REGISTRY_LISTS = {
    "config/area_registry/list": ("area_registry", "async_list_areas"),
    "config/floor_registry/list": ("floor_registry", "async_list_floors"),
    "config/label_registry/list": ("label_registry", "async_list_labels"),
}
RAW_COMMANDS = frozenset(
    {
        *_REGISTRY_LISTS,
        "config/device_registry/list",
        "config/entity_registry/list",
        "config/entity_registry/get",
        "get_config",
        "get_states",
        "get_services",
        "network/url",
        "repairs/list_issues",
        "repairs/get_issue_data",
    }
)


def _registry(hass: Any, name: str) -> Any:
    return import_module(f"homeassistant.helpers.{name}").async_get(hass)


def _registry_body(hass: Any, command: str, values: dict[str, Any]) -> Any:
    if command in _REGISTRY_LISTS:
        name, method = _REGISTRY_LISTS[command]
        return [
            entry.json_fragment for entry in getattr(_registry(hass, name), method)()
        ]
    if command == "config/device_registry/list":
        owner = _registry(hass, "device_registry")
        return [
            json.loads(entry.json_repr)
            for entry in chain(owner.devices, owner.child_devices)
        ]
    if command == "config/entity_registry/list":
        owner = _registry(hass, "entity_registry")
        return [
            entry.as_partial_dict
            for entry in owner.entities.values()
            if entry.partial_json_repr is not None
        ]
    if command == "config/entity_registry/get":
        entry = _registry(hass, "entity_registry").async_get(values["entity_id"])
        if entry is None:
            raise ToolFailure(
                "ENTITY_NOT_FOUND", "Entity not found", status="not_started"
            )
        return entry.extended_dict
    raise UnsupportedOperation(command)


async def _command_body(
    hass: Any, command: str, values: dict[str, Any], actor: CallContext
) -> Any:
    if command.startswith("config/"):
        return _registry_body(hass, command, values)
    if command == "network/url":
        return _network_urls(hass)
    if command == "repairs/list_issues":
        from .websocket_api.overview import _overview_repairs

        _registry(hass, "issue_registry")
        return {"issues": _overview_repairs(hass)}
    if command == "repairs/get_issue_data":
        issue = _registry(hass, "issue_registry").async_get_issue(
            values["domain"], values["issue_id"]
        )
        if issue is None:
            raise ToolFailure(
                "unknown_issue",
                f"Issue '{values['issue_id']}' not found",
                status="not_started",
            )
        return {"issue_data": issue.data}
    if command == "get_services":
        from homeassistant.helpers.service import async_get_all_descriptions

        return await async_get_all_descriptions(hass)
    if command == "get_config":
        user = await hass.auth.async_get_user(actor.user_id)
        result = dict(hass.config.as_dict())
        if getattr(user, "local_only", False):
            result.pop("external_url", None)
        return result
    return await _api_body(hass, "states")


def _network_urls(hass: Any) -> dict[str, Any]:
    from homeassistant.helpers.network import NoURLAvailableError, get_url

    result = dict.fromkeys(("internal", "external", "cloud"))
    with suppress(NoURLAvailableError):
        result["internal"] = get_url(
            hass, allow_internal=True, allow_external=False, allow_cloud=False
        )
    with suppress(NoURLAvailableError):
        result["external"] = get_url(
            hass, allow_internal=False, allow_external=True, prefer_external=True
        )
    with suppress(NoURLAvailableError):
        result["cloud"] = get_url(hass, allow_internal=False, require_cloud=True)
    return result


async def execute_command(
    backend: Any, arguments: dict[str, Any], actor: CallContext
) -> dict[str, Any]:
    command, payload = validate_ws_request(
        arguments["ws_command"],
        arguments.get("data"),
        domain=arguments.get("domain"),
        service=arguments.get("service"),
    )
    if command not in RAW_COMMANDS:
        raise UnsupportedOperation(
            f"native command {command}; use a dedicated tool or the legacy runtime"
        )
    values = _validated_payload(backend.hass, command, payload)
    await _authorize_read(backend, actor)
    result = json.loads(
        json_bytes(await _command_body(backend.hass, command, values, actor))
    )
    await _authorize_read(backend, actor)
    return {"success": True, "ws_command": command, "result": result}


async def _api_body(hass: Any, path: str) -> Any:
    if path == "config":
        return hass.config.as_dict()
    if path == "states":
        return [state.as_dict() for state in hass.states.async_all()]
    if path.startswith("states/") and path.count("/") == 1:
        state = hass.states.get(path.removeprefix("states/"))
        if state is None:
            raise ToolFailure(
                "ENTITY_NOT_FOUND", "Entity not found.", status="not_started"
            )
        return state.as_dict()
    if path == "services":
        from homeassistant.components.api import async_services_json

        return await async_services_json(hass)
    raise UnsupportedOperation(f"native API GET /{path}")


async def execute_get(backend: Any, path: str, actor: CallContext) -> Any:
    await _authorize_read(backend, actor)
    result = json.loads(json_bytes(await _api_body(backend.hass, path)))
    await _authorize_read(backend, actor)
    return result


async def _authorize_read(backend: Any, actor: CallContext) -> None:
    await backend.authorize(actor)
    if admission := current_admission.get():
        if admission.context != actor:
            raise PermissionError("A raw read cannot change the admitted actor")
        admission.check_policy()


async def execute_template(backend: Any, data: Any, actor: CallContext) -> Any:
    from homeassistant.components.api import _cached_template

    if not isinstance(data, dict) or "template" not in data:
        raise ToolFailure(
            "INVALID_ARGUMENTS",
            "Template data must contain a template.",
            status="not_started",
        )
    await _authorize_read(backend, actor)
    template = _cached_template(data["template"], backend.hass)
    variables = data.get("variables")
    if await await_owned_io(template.async_render_will_timeout(3, variables)):
        raise ToolFailure(
            "TEMPLATE_ERROR",
            "Exceeded maximum template execution time",
            status="not_started",
        )
    await _authorize_read(backend, actor)
    rendered = template.async_render(variables=variables, parse_result=False)
    await _authorize_read(backend, actor)
    # The legacy API helper decodes JSON first, then preserves non-JSON text.
    try:
        return json.loads(rendered)
    except (ValueError, TypeError):
        return rendered
