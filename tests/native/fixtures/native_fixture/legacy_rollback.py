"""Switch one disposable entry between runtimes without changing its artifacts.

Hook POST legacy_rollback -> await legacy_rollback(hass, user, body).
Secrets stay in the fixture's private in-memory state and config entry; every
response contains only booleans, public runtime observations and phase labels.
"""

import copy
import hashlib
import importlib.metadata
import json
import threading
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import psutil
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.helpers import llm

from custom_components.ha_mcp_tools.const import (
    BIND_HOST_LOOPBACK,
    DATA_ADMIN_TOKEN,
    DATA_ISSUED_TOKEN_ID,
    DATA_MANAGER,
    DATA_WEBHOOK,
    DATA_WEBHOOK_ID,
    DOMAIN,
    OPT_BIND_HOST,
    OPT_PIP_SPEC,
    OPT_RUNTIME_MODE,
    OPT_SERVER_PORT,
    RUNTIME_LEGACY,
    RUNTIME_NATIVE,
)

_FIXTURE = "native_fixture"
_KEY = "legacy_rollback"
_PORT = 9584
_ENTITY = "input_boolean.native_light"


@dataclass(slots=True, repr=False)
class _Rollback:
    entry_id: str
    unique_id: str | None
    data: dict[str, Any]
    options: dict[str, Any]
    artifacts: str
    llm_ids: set[str]
    entity_state: str
    user_id: str
    phase: str = "legacy"
    issued_token_id: str | None = None


def _artifacts(component: Path) -> str:
    distribution = importlib.metadata.distribution("ha-mcp")
    digest = hashlib.sha256()
    digest.update(distribution.version.encode())
    record = distribution.read_text("RECORD")
    if record is None:
        raise AssertionError("The installed candidate wheel has no RECORD")
    digest.update(record.encode())
    installed = sorted(
        (distribution.metadata["Name"], distribution.version)
        for distribution in importlib.metadata.distributions()
    )
    digest.update(json.dumps(installed).encode())
    for path in sorted(component.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        digest.update(path.relative_to(component).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _listeners() -> set[int]:
    return {
        connection.laddr.port
        for connection in psutil.Process().net_connections(kind="inet")
        if connection.status == "LISTEN"
    }


def _legacy_threads() -> bool:
    return any(thread.name == "ha-mcp-server" for thread in threading.enumerate())


def _llm_ids(hass: Any, entry_id: str) -> set[str]:
    prefix = f"ha_mcp_tools-{entry_id}"
    return {api.id for api in llm.async_get_apis(hass) if api.id.startswith(prefix)}


async def _status(hass: Any, saved: _Rollback) -> dict[str, Any]:
    entry = hass.config_entries.async_get_entry(saved.entry_id)
    if entry is None:
        raise AssertionError("Runtime switching removed the original entry")
    domain = hass.data.get(DOMAIN, {})
    manager = domain.get(DATA_MANAGER)
    cfg = domain.get(DATA_WEBHOOK)
    expected = (
        "EmbeddedServerManager" if saved.phase == "legacy" else "NativeServerManager"
    )
    ids = _llm_ids(hass, saved.entry_id)
    ready = bool(
        manager
        and manager.is_running
        and type(manager).__name__ == expected
        and cfg
        and ids == saved.llm_ids
    )
    artifact_match = False
    if ready:
        artifact_match = saved.artifacts == await hass.async_add_executor_job(
            _artifacts, Path(hass.config.path("custom_components", DOMAIN))
        )
    state = hass.states.get(_ENTITY)
    return {
        "phase": saved.phase,
        "ready": ready,
        "entry_preserved": entry.unique_id == saved.unique_id,
        "webhook_preserved": bool(
            cfg and cfg["webhook_id"] == saved.data[DATA_WEBHOOK_ID]
        ),
        "data_preserved": all(
            entry.data.get(key) == value for key, value in saved.data.items()
        ),
        "native_options_restored": dict(entry.options) == saved.options,
        "artifacts_unchanged": artifact_match,
        "llm_ids_preserved": ids == saved.llm_ids,
        "state_preserved": state is not None and state.state == saved.entity_state,
        "legacy_thread": _legacy_threads(),
        "legacy_listener": _PORT in await hass.async_add_executor_job(_listeners),
        "guard_enabled": hass.data[_FIXTURE]["guard"],
        "fixture_token_revoked": saved.issued_token_id is not None
        and hass.auth.async_get_refresh_token(saved.issued_token_id) is None,
        "fixture_credential_removed": DATA_ADMIN_TOKEN not in entry.data
        and DATA_ISSUED_TOKEN_ID not in entry.data,
    }


async def _start(hass: Any, user: Any) -> _Rollback:
    fixture = hass.data[_FIXTURE]
    if _KEY in fixture:
        raise AssertionError("Rollback qualification has already started")
    manager = hass.data[DOMAIN][DATA_MANAGER]
    if type(manager).__name__ != "NativeServerManager" or not manager.is_running:
        raise AssertionError("Rollback qualification requires an active native entry")
    entry = manager.entry
    if DATA_ADMIN_TOKEN in entry.data or DATA_ISSUED_TOKEN_ID in entry.data:
        raise AssertionError(
            "The disposable native entry already has a legacy credential"
        )
    state = hass.states.get(_ENTITY)
    if (
        state is None
        or not user.is_admin
        or not user.is_active
        or user.system_generated
    ):
        raise AssertionError("Disposable rollback actor or state is unavailable")
    saved = _Rollback(
        entry.entry_id,
        entry.unique_id,
        copy.deepcopy(dict(entry.data)),
        copy.deepcopy(dict(entry.options)),
        await hass.async_add_executor_job(
            _artifacts, Path(hass.config.path("custom_components", DOMAIN))
        ),
        _llm_ids(hass, entry.entry_id),
        state.state,
        user.id,
    )
    fixture[_KEY] = saved
    if not await hass.config_entries.async_unload(entry.entry_id):
        raise AssertionError("Native entry did not unload before rollback")
    credential = await hass.auth.async_create_refresh_token(
        user,
        client_name=f"Native rollback fixture {uuid4().hex}",
        token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
        access_token_expiration=timedelta(days=1),
    )
    saved.issued_token_id = credential.id
    hass.config_entries.async_update_entry(
        entry,
        data={
            **saved.data,
            DATA_ADMIN_TOKEN: hass.auth.async_create_access_token(credential),
            DATA_ISSUED_TOKEN_ID: credential.id,
        },
        options={
            **saved.options,
            OPT_RUNTIME_MODE: RUNTIME_LEGACY,
            OPT_SERVER_PORT: _PORT,
            OPT_BIND_HOST: BIND_HOST_LOOPBACK,
            OPT_PIP_SPEC: "",
        },
    )
    fixture["guard"] = False
    if not await hass.config_entries.async_setup(entry.entry_id):
        raise AssertionError("Legacy entry setup refused the same candidate")
    return saved


async def _return_native(hass: Any, saved: _Rollback) -> None:
    if saved.phase == "native":
        return
    entry = hass.config_entries.async_get_entry(saved.entry_id)
    if entry is None:
        raise AssertionError("The original entry is missing")
    if not await hass.config_entries.async_unload(entry.entry_id):
        raise AssertionError("Legacy entry did not unload")
    if _legacy_threads() or _PORT in await hass.async_add_executor_job(_listeners):
        raise AssertionError(
            "Legacy resources survived unload; token is not revoked yet"
        )
    if (
        saved.issued_token_id is not None
        and (credential := hass.auth.async_get_refresh_token(saved.issued_token_id))
        is not None
    ):
        if credential.user.id != saved.user_id:
            raise AssertionError("Fixture credential ownership changed")
        hass.auth.async_remove_refresh_token(credential)
    hass.config_entries.async_update_entry(
        entry,
        data=saved.data,
        options={**saved.options, OPT_RUNTIME_MODE: RUNTIME_NATIVE},
    )
    hass.data[_FIXTURE]["guard"] = True
    saved.phase = "native"
    if not await hass.config_entries.async_setup(entry.entry_id):
        raise AssertionError("Native entry setup refused after legacy teardown")


async def legacy_rollback(hass: Any, user: Any, body: dict[str, Any]) -> dict[str, Any]:
    action = body.get("action")
    if action == "start":
        saved = await _start(hass, user)
    else:
        saved = hass.data[_FIXTURE].get(_KEY)
        if saved is None or saved.user_id != user.id:
            raise AssertionError("No rollback state belongs to this fixture caller")
        if action == "return":
            await _return_native(hass, saved)
        elif action != "status":
            raise ValueError("Unknown rollback fixture action")
    return await _status(hass, saved)
