"""The registry-backup E2E oracle must observe restoration, not a success ACK."""

import copy

import pytest

from ..e2e.utilities import entity_registry_backup
from ..e2e.workflows.auto_backup import test_capture_and_restore as scenarios


class RegistryBackupClient:
    def __init__(self, hidden_by=None, *, capture=True, apply_restore=True):
        self.entry = {
            "entity_id": "light.bed_light",
            "hidden_by": hidden_by,
            "hidden": hidden_by is not None,
            "name": "Original light",
            "labels": ["retained"],
        }
        self.before = copy.deepcopy(self.entry)
        self.capture = capture
        self.apply_restore = apply_restore
        self.writes = []
        self.restores = []
        self.backups = {
            "prior.json": {
                "domain": "entity_registry",
                "entity_id": "another-registration",
                "config": {"entity_id": "light.other", "hidden_by": None},
            }
        }
        self.sequence = 0

    def snapshot(self):
        self.sequence += 1
        name = f"capture-{self.sequence}.json"
        self.backups[name] = {
            "domain": "entity_registry",
            "entity_id": "durable-registry-id",
            "config": {"id": "durable-registry-id", **copy.deepcopy(self.entry)},
        }
        return name

    async def call_tool(self, tool, arguments):
        if tool == "ha_get_state":  # The former test looked at state, not registry.
            return {"success": True, "data": {"state": "on"}}
        if tool == "ha_get_entity":
            return {"success": True, "entity_entry": copy.deepcopy(self.entry)}
        if tool == "ha_set_entity":
            self.writes.append(arguments["hidden"])
            if self.capture:
                self.snapshot()
            self.entry.update(
                hidden=arguments["hidden"],
                hidden_by="user" if arguments["hidden"] else None,
            )
            return {"success": True}
        assert tool == "ha_manage_backup"
        action = arguments["action"]
        if action == "list":
            return {
                "success": True,
                "data": {
                    "backups": [
                        {
                            "name": name,
                            "domain": data["domain"],
                            "entity_id": data["entity_id"],
                        }
                        for name, data in self.backups.items()
                        if data["domain"] == arguments.get("domain", data["domain"])
                        and data["entity_id"]
                        == arguments.get("entity_id", data["entity_id"])
                    ]
                },
            }
        name = arguments["backup_name"]
        if action == "view":
            return {"success": True, "data": copy.deepcopy(self.backups[name])}
        if action == "restore":
            self.restores.append(name)
            safety = self.snapshot()
            if self.apply_restore:
                saved = self.backups[name]["config"]
                self.entry.update({key: saved[key] for key in self.entry})
            return {"success": True, "data": {"safety_backup": safety}}
        assert action == "delete"
        del self.backups[name]
        return {"success": True}


@pytest.fixture(autouse=True)
def bound_poll_window(monkeypatch):
    original = entity_registry_backup.wait_for_tool_result

    async def wait(*args, **kwargs):
        return await original(
            *args, **{**kwargs, "timeout": 0.01, "poll_interval": 0.001}
        )

    monkeypatch.setattr(entity_registry_backup, "wait_for_tool_result", wait)


async def scenario(client, monkeypatch):
    try:
        await (
            scenarios.TestEntityRegistryCaptureRestore().test_entity_registry_full_loop(
                client, monkeypatch
            )
        )
    except pytest.skip.Exception as error:
        pytest.fail(f"A registry capture defect was hidden as a skip: {error}")


@pytest.mark.parametrize("hidden_by", [None, "user"])
async def test_registry_backup_scenario_restores_prior_visibility_and_keeps_unowned_history(
    monkeypatch, hidden_by
):
    client = RegistryBackupClient(hidden_by)
    await scenario(client, monkeypatch)
    assert client.entry == client.before
    assert client.writes == [hidden_by is None]
    assert len(client.restores) == 1
    assert list(client.backups) == ["prior.json"]


async def test_missing_registry_capture_fails_and_restores_the_initial_visibility(
    monkeypatch,
):
    client = RegistryBackupClient(capture=False)
    with pytest.raises(TimeoutError):
        await scenario(client, monkeypatch)
    assert client.entry == client.before
    assert client.writes == [True, False]


async def test_successful_restore_ack_without_registry_effect_is_rejected(monkeypatch):
    client = RegistryBackupClient(apply_restore=False)
    with pytest.raises(TimeoutError):
        await scenario(client, monkeypatch)
    assert client.entry == client.before
    assert len(client.restores) == 1, "An unconfirmed restore was replayed"
    assert list(client.backups) == ["prior.json"]
