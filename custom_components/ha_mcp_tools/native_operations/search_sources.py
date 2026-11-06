"""Read Search's persisted bodies once per real Core config owner."""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ha_mcp.runtime.search_response import _ResolvedSearch
from ha_mcp.tool_api import ToolFailure
from ha_mcp.utils.owned_executor import await_owned_io

from ..native_server_policy import setting_value
from ..websocket_api.search_config import _extract_config, _too_large
from .core_views import config_view
from .registry_access import registry_json


@dataclass(slots=True)
class ConfigBodies:
    stored: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SearchCandidates:
    bodies: dict[str, Any] = field(default_factory=dict)
    corpora: dict[str, Any] = field(default_factory=dict)
    entities: dict[str, Any] = field(default_factory=dict)
    omitted: list[str] = field(default_factory=list)

    def collect(self, row: dict[str, Any]) -> None:
        key = row.get("entity_id") or row.get("entry_id")
        if not key:
            return
        if "_match_texts" in row:
            self.entities[key] = row
            return
        body = row.get("config", row.get("options"))
        self.corpora[key] = body
        if row.get("helper_type") and body is not None:
            self.add_body(key, body)

    def add_body(self, identity: str, body: dict[str, Any]) -> None:
        if _too_large(body):
            self.omitted.append(identity)
        else:
            self.bodies[identity] = body

    def include_persisted(self, stored: ConfigBodies) -> None:
        for items in stored.stored.values():
            for eid, body in items.items():
                self.add_body(eid, body)


def config_budget(manager: Any, req: _ResolvedSearch, domain: str) -> float:
    value = req.config_time_budget
    if value is None:
        value = search_setting(manager, f"{domain}_config_time_budget")
    return float(value)


def search_setting(manager: Any, name: str) -> int | float:
    from ha_mcp.config_meta import setting_of
    from ha_mcp.config_settings import Settings, _parse_lenient

    field = Settings.model_fields[name]
    metadata = setting_of(field)
    assert metadata is not None
    value, problem = _parse_lenient(
        setting_value(manager.policy.flags, name),
        metadata,
        whole=field.annotation is int,
    )
    if problem:
        if metadata.lenient:
            return float(field.default)
        raise ToolFailure("INVALID_SETTING", f"Invalid {name}: {problem}")
    assert isinstance(value, int | float)
    return value


async def _domain_bodies(hass: Any, domain: str) -> dict[str, dict[str, Any]]:
    owner = config_view(hass, domain)
    # Keep Core's lock until its owned read finishes, including cancellation.
    async with owner.mutation_lock:
        current = await await_owned_io(owner.read_config(hass))
        result = {}
        for entity in getattr(hass.data.get(domain), "entities", ()):
            _, item_id, _ = _extract_config(domain, entity)
            if item_id is None:
                continue
            value = owner._get_value(hass, current, item_id)
            if isinstance(value, dict):
                result[entity.entity_id] = registry_json(value)
        return result


async def load_config_bodies(
    manager: Any, req: _ResolvedSearch, domains: list[str], check: Any
) -> ConfigBodies:
    result = ConfigBodies()
    for domain in domains:
        if domain not in {"automation", "script", "scene"}:
            continue
        try:
            async with asyncio.timeout(config_budget(manager, req, domain)):
                result.stored[domain] = await _domain_bodies(manager.hass, domain)
        except ToolFailure as error:
            if error.code == "AUTH_FORBIDDEN":
                raise
            result.reasons.append(
                f"{domain} persisted configuration unavailable; matching uses scrubbed loaded configuration and bodies are withheld"
            )
        except Exception:  # noqa: BLE001 - one inaccessible config owner leaves the other surfaces useful
            result.reasons.append(
                f"{domain} persisted configuration unavailable; matching uses scrubbed loaded configuration and bodies are withheld"
            )
        await check()
    return result
