"""Historical entity files restore StateMachine data, never a registry or device."""

import asyncio
import copy
import importlib
import json
import sys
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from ha_mcp.runtime.snapshot_payloads import BackupRestoreError
from ha_mcp.tool_api import CallContext

from .test_native_snapshot_restore import runtime as runtime


class _InvalidEntityFormat(ValueError):
    pass


class _InvalidState(ValueError):
    pass


def _validate_state(value):
    if value == "core-rejects-this-state":
        raise _InvalidState("Core rejected this state")
    return value


@dataclass
class _Context:
    user_id: str | None = None


class _State:
    def __init__(self, entity_id, state, attributes=None, *, context=None):
        self.entity_id, self.state = entity_id, state
        self.attributes, self.context = (
            attributes or {},
            context or _Context("provider"),
        )
        self.revision = 0

    def as_dict(self):
        return {
            "entity_id": self.entity_id,
            "state": self.state,
            "attributes": copy.deepcopy(self.attributes),
            "last_updated": self.revision,
            "context": {"user_id": self.context.user_id},
        }


class _States:
    def __init__(self):
        self.rows = {"sensor.legacy": _State("sensor.legacy", "current", {"unit": "W"})}
        self.calls = []
        self.fail_after = False

    def get(self, entity_id):
        return self.rows.get(entity_id)

    def async_set(self, entity_id, value, attributes, *, context):
        self.calls.append((entity_id, value, copy.deepcopy(attributes), context))
        self.rows[entity_id] = _State(entity_id, value, attributes, context=context)
        self.rows[entity_id].revision = 1
        if self.fail_after:
            raise ValueError("State listener failed")


@pytest.fixture
def state_history(runtime, monkeypatch):
    manager, snapshots, store, *_ = runtime
    states = _States()
    manager.hass.states = states
    for name, value in (
        ("State", _State),
        ("Context", _Context),
        ("validate_state", _validate_state),
    ):
        monkeypatch.setattr(
            sys.modules["homeassistant.core"], name, value, raising=False
        )
    exceptions = sys.modules.get("homeassistant.exceptions", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "homeassistant.exceptions", exceptions)
    monkeypatch.setattr(exceptions, "InvalidStateError", _InvalidState, raising=False)
    monkeypatch.setattr(
        exceptions, "InvalidEntityFormatError", _InvalidEntityFormat, raising=False
    )
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.state_snapshots"
    )
    for name, value in (
        ("State", _State),
        ("Context", _Context),
        ("InvalidStateError", _InvalidState),
        ("InvalidEntityFormatError", _InvalidEntityFormat),
        ("validate_state", _validate_state),
    ):
        monkeypatch.setattr(module, name, value, raising=False)
    access = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.registry_access"
    )
    monkeypatch.setattr(access, "json_bytes", lambda value: json.dumps(value).encode())
    saved = {
        "entity_id": "sensor.legacy",
        "state": "saved",
        "attributes": {"unit": "kW"},
        "last_updated": "never replay this",
        "context": {"user_id": "old-server-admin"},
    }
    path = store.capture("entity", "sensor.legacy", saved, "fixture")
    return manager, snapshots, store, path, states


async def test_old_entity_snapshot_reads_real_state_and_restores_only_state_attributes(
    state_history,
):
    _, snapshots, store, path, states = state_history
    before = states.rows["sensor.legacy"].as_dict()
    data, current = await snapshots.snapshot_comparison(path.name, CallContext("owner"))
    assert current == before and data["config"]["state"] == "saved"
    result = await asyncio.wait_for(
        snapshots.restore_snapshot(path.name, CallContext("owner")), 2
    )
    assert (
        result["apply_status"] == "applied" and result["temporary_state_only"] is True
    )
    assert states.rows["sensor.legacy"].state == "saved"
    assert states.rows["sensor.legacy"].attributes == {"unit": "kW"}
    assert states.calls[0][3].user_id == "owner"
    assert (
        states.rows["sensor.legacy"].as_dict()["last_updated"]
        != data["config"]["last_updated"]
    )
    assert store.read_snapshot(result["safety_backup"])["config"] == before
    assert len(states.calls) == 1 and "automations" in result["warnings"][0]
    assert "physical device state" in result["warnings"][0]


async def test_missing_old_entity_can_recreate_only_a_temporary_state_value(
    state_history,
):
    _, snapshots, _, path, states = state_history
    states.rows.clear()
    result = await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert result["apply_status"] == "applied" and result["safety_backup"] is None
    assert (
        result["temporary_state_only"] is True
        and states.rows["sensor.legacy"].state == "saved"
    )
    assert len(states.calls) == 1


@pytest.mark.parametrize("change", ["state", "owner", "read_only", "revoke"])
async def test_old_entity_restore_refuses_changes_or_rights_loss_after_safety_io(
    state_history, monkeypatch, change
):
    manager, snapshots, _, path, states = state_history
    io = snapshots._io

    async def changed(function, *args):
        value = await io(function, *args)
        if function.__name__ == "capture":
            if change == "state":
                states.rows["sensor.legacy"].state = "new provider value"
            elif change == "owner":
                replacement = _States()
                replacement.rows = copy.deepcopy(states.rows)
                manager.hass.states = replacement
            elif change == "read_only":
                manager.policy.flags["backup_read_only"] = True
            else:
                manager.backend.authorize.side_effect = PermissionError("revoked")
        return value

    monkeypatch.setattr(snapshots, "_io", changed)
    with pytest.raises(BackupRestoreError) as raised:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert raised.value.outcome["apply_status"] == "not_applied"
    assert raised.value.outcome["safety_backup"] and states.calls == []
    assert "physical device state" in raised.value.outcome["warnings"][0]


async def test_old_entity_state_listener_failure_is_unknown_without_registry_recreation(
    state_history,
):
    _, snapshots, _, path, states = state_history
    states.fail_after = True
    with pytest.raises(BackupRestoreError) as raised:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert raised.value.outcome["apply_status"] == "unknown"
    assert raised.value.outcome["temporary_state_only"] is True
    assert states.rows["sensor.legacy"].state == "saved" and len(states.calls) == 1


@pytest.mark.parametrize(
    "config",
    [{"attributes": {}}, {"state": None}, {"state": "core-rejects-this-state"}],
)
async def test_invalid_old_entity_state_is_rejected_before_the_state_owner_callback(
    state_history, config
):
    _, snapshots, store, _, states = state_history
    path = store.capture("entity", "sensor.legacy", config, "fixture")
    with pytest.raises(BackupRestoreError) as raised:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert raised.value.outcome["apply_status"] == "not_applied"
    assert states.calls == [] and states.rows["sensor.legacy"].state == "current"
