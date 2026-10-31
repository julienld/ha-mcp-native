"""Local Core backup setup and cleanup limited to disposable fixture archives."""

import json
import secrets
from typing import Any
from uuid import uuid4

from homeassistant.components.backup import async_get_manager
from homeassistant.core import HomeAssistant
from homeassistant.helpers.json import json_bytes

DOMAIN = "native_fixture"


async def seed_backups(hass: HomeAssistant) -> dict[str, Any]:
    owner = async_get_manager(hass)
    agents = [
        key for key, agent in owner.backup_agents.items() if agent.name == "local"
    ]
    assert agents and "backup.local" in agents, agents
    state = hass.data[DOMAIN].setdefault(
        "backup_probe",
        {"prefix": f"Native_fixture_backup_{uuid4().hex}_", "ids": set()},
    )
    owner.config.update(
        create_backup={
            "password": secrets.token_urlsafe(32),
            "agent_ids": ["backup.local"],
        }
    )
    assert hass.states.get("sensor.native_energy") is not None
    return {
        "prefix": state["prefix"],
        "agent_id": "backup.local",
        "state_entity": "sensor.native_energy",
    }


async def backup_probe(hass: HomeAssistant, backup_id: str) -> dict[str, Any]:
    state = hass.data[DOMAIN]["backup_probe"]
    backup, errors = await async_get_manager(hass).async_get_backup(backup_id)
    assert not errors, {key: type(error).__name__ for key, error in errors.items()}
    if backup is None:
        return {"present": False}
    assert backup.name.startswith(state["prefix"]), (
        "Refusing to inspect a non-fixture archive"
    )
    state["ids"].add(backup_id)
    return {"present": True, "backup": json.loads(json_bytes(backup))}


async def cleanup_backups(hass: HomeAssistant) -> dict[str, Any]:
    state = hass.data[DOMAIN]["backup_probe"]
    owner = async_get_manager(hass)
    rows, errors = await owner.async_get_backups()
    assert not errors, {key: type(error).__name__ for key, error in errors.items()}
    removed = []
    for backup_id, backup in rows.items():
        if backup_id not in state["ids"] or not backup.name.startswith(state["prefix"]):
            continue
        failures = await owner.async_delete_backup(backup_id)
        assert not failures, {
            key: type(error).__name__ for key, error in failures.items()
        }
        removed.append(backup_id)
    return {"removed": removed}
