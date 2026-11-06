"""The entity restore scenarios must select the history that the writer creates."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ..e2e.workflows.auto_backup import test_entity_capture_restore as scenarios


class BackupSelected(Exception):
    """Stop after observing the scenario's history lookup, before restoration."""


@pytest.mark.parametrize(
    "scenario",
    [
        "test_restore_renames_back_and_unhides",
        "test_deleted_registry_restore_refuses_without_recreating_the_entity",
    ],
)
async def test_entity_restore_scenario_selects_the_public_entity_history(
    monkeypatch, scenario
):
    # Registry UUID remains inside the image; history is selected using the
    # public entity ID captured by the writer, distinct from that UUID.
    registry_id = "44ca272a46cb46eb850bad205bc54568"
    entry = {"hidden": False}

    async def call_tool(name, arguments):
        if name == "ha_get_entity":
            return {"success": True, "entity_entry": entry}
        if name == "ha_get_entity_exposure":
            return {"success": True, "exposed_to": {"conversation": True}}
        assert name in {
            "ha_config_set_helper",
            "ha_set_entity",
            "ha_remove_helpers_integrations",
        }
        return {"success": True}

    client = SimpleNamespace(call_tool=AsyncMock(side_effect=call_tool))
    core = SimpleNamespace(
        send_websocket_message=AsyncMock(
            return_value={"success": True, "result": {"id": registry_id}}
        )
    )
    lookup = AsyncMock(side_effect=BackupSelected)
    monkeypatch.setattr(scenarios, "_wait_for_backup", lookup)
    with pytest.raises(BackupSelected):
        await getattr(scenarios.TestEntityCaptureRestore(), scenario)(
            client, core, monkeypatch
        )
    core.send_websocket_message.assert_awaited_once()
    registry_read = core.send_websocket_message.call_args.args[0]
    assert registry_read["type"] == "config/entity_registry/get"
    assert registry_read["entity_id"] != registry_id
    lookup.assert_awaited_once_with(
        client, domain="entity", entity_id=registry_read["entity_id"]
    )
