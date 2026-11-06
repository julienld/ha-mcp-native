"""E2E: an entity edit backup restores the entity's registry settings and ID."""

from __future__ import annotations

import uuid

import pytest

from ...utilities.assertions import MCPAssertions, safe_call_tool
from ...utilities.wait_helpers import wait_for_tool_result
from .test_capture_and_restore import (
    _delete_leftover_backup,
    _enable_auto_backup,
    _wait_for_backup,
)


async def _registry_id(ha_client, entity_id: str) -> str:
    # ha_get_entity projects user-facing fields; the backup key is Core's UUID.
    response = await ha_client.send_websocket_message(
        {"type": "config/entity_registry/get", "entity_id": entity_id}
    )
    assert response["success"], response
    identity = response["result"]["id"]
    assert isinstance(identity, str) and identity, response
    return identity


@pytest.mark.convenience
@pytest.mark.cleanup
@pytest.mark.external_only
class TestEntityCaptureRestore:
    """``ha_set_entity`` writes the entity registry, so its backup must bring
    registry settings back. The lane edits a disposable input_boolean rather
    than a demo entity other lanes use, and renames it, which the restore must
    undo through the registry entry's stable id. The edit also flips the
    entity's Assist exposure, kept in the registry entry's options, which the
    restore must reset; and as it re-enables nothing, the restore must not ask
    for a restart."""

    async def test_restore_renames_back_and_unhides(
        self, mcp_client, ha_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        helper_id = f"e2e_bk_ent_{uuid.uuid4().hex[:8]}"
        entity_id = f"input_boolean.{helper_id}"
        renamed = f"{entity_id}_renamed"
        backup_name = None
        async with MCPAssertions(mcp_client) as mcp:
            await mcp.call_tool_success(
                "ha_config_set_helper",
                {"helper_type": "input_boolean", "name": helper_id},
            )
            try:
                await wait_for_tool_result(
                    mcp_client,
                    tool_name="ha_get_entity",
                    arguments={"entity_id": entity_id},
                    predicate=lambda d: d["entity_entry"]["hidden"] is False,
                    description=f"{entity_id} in the entity registry",
                )
                registry_id = await _registry_id(ha_client, entity_id)
                exposure = await mcp.call_tool_success(
                    "ha_get_entity_exposure", {"entity_id": entity_id}
                )
                exposed = exposure["exposed_to"]["conversation"]
                # The edit flips the exposure read above, so the read-back after
                # the restore fails if the restore leaves exposure as edited.
                await mcp.call_tool_success(
                    "ha_set_entity",
                    {
                        "entity_id": entity_id,
                        "hidden": True,
                        "new_entity_id": renamed,
                        "expose_to": {"conversation": not exposed},
                    },
                )
                backup_name = await _wait_for_backup(
                    mcp_client, domain="entity", entity_id=entity_id
                )
                # The edit took, so each read-back below tests the restore.
                edited = await mcp.call_tool_success(
                    "ha_get_entity", {"entity_id": renamed}
                )
                assert edited["entity_entry"]["hidden"] is True
                exposure = await mcp.call_tool_success(
                    "ha_get_entity_exposure", {"entity_id": renamed}
                )
                assert exposure["exposed_to"]["conversation"] is (not exposed)

                restore = await mcp.call_tool_success(
                    "ha_manage_backup",
                    {"scope": "edits", "action": "restore", "backup_name": backup_name},
                )
                # The safety backup must find the entity under its new ID.
                assert restore["data"]["safety_backup"] is not None
                # Core 2026.10 reports require_restart for any update that sends
                # disabled_by=None to an entity without a config entry, such as
                # this helper, although it was never disabled.
                assert not restore["data"]["result"].get("require_restart")
                # A restore that only reset the state, as it once did, leaves
                # the entity renamed and hidden: the old ID is then not found.
                restored = await mcp.call_tool_success(
                    "ha_get_entity", {"entity_id": entity_id}
                )
                assert restored["entity_entry"]["hidden"] is False
                assert await _registry_id(ha_client, entity_id) == registry_id
                exposure = await mcp.call_tool_success(
                    "ha_get_entity_exposure", {"entity_id": entity_id}
                )
                assert exposure["exposed_to"]["conversation"] is exposed
            finally:
                for target in (entity_id, renamed):
                    await safe_call_tool(
                        mcp_client,
                        "ha_remove_helpers_integrations",
                        {"target": target, "confirm": True},
                    )
                await _delete_leftover_backup(mcp_client, backup_name)

    async def test_deleted_registry_restore_refuses_without_recreating_the_entity(
        self, mcp_client, ha_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """History preserves the saved UUID but cannot recreate its registration."""
        _enable_auto_backup(monkeypatch)
        helper_id = f"e2e_bk_gone_{uuid.uuid4().hex[:8]}"
        entity_id = f"input_boolean.{helper_id}"
        backup_name = None
        async with MCPAssertions(mcp_client) as mcp:
            await mcp.call_tool_success(
                "ha_config_set_helper",
                {"helper_type": "input_boolean", "name": helper_id},
            )
            try:
                await wait_for_tool_result(
                    mcp_client,
                    tool_name="ha_get_entity",
                    arguments={"entity_id": entity_id},
                    predicate=lambda d: d["entity_entry"]["hidden"] is False,
                    description=f"{entity_id} in the entity registry",
                )
                registry_id = await _registry_id(ha_client, entity_id)
                await mcp.call_tool_success(
                    "ha_set_entity", {"entity_id": entity_id, "icon": "mdi:test-tube"}
                )
                backup_name = await _wait_for_backup(
                    mcp_client, domain="entity", entity_id=entity_id
                )
                await mcp.call_tool_success(
                    "ha_remove_helpers_integrations",
                    {"target": entity_id, "confirm": True},
                )

                result = await mcp.call_tool_failure(
                    "ha_manage_backup",
                    {"scope": "edits", "action": "restore", "backup_name": backup_name},
                )

                assert result["error"]["code"] == "RESOURCE_NOT_FOUND", result
                outcome = result["data"]
                assert outcome["reason"] == "entry_deleted", result
                assert outcome["apply_status"] == "not_applied", result
                assert outcome["verification_status"] == "not_run", result
                assert outcome["safety_backup"] is None, result
                saved = await mcp.call_tool_success(
                    "ha_manage_backup",
                    {"scope": "edits", "action": "view", "backup_name": backup_name},
                )
                assert saved["data"]["config"]["id"] == registry_id, saved
                current = await ha_client.send_websocket_message(
                    {"type": "config/entity_registry/list"}
                )
                assert current["success"], current
                assert not any(
                    row["id"] == registry_id or row["entity_id"] == entity_id
                    for row in current["result"]
                ), current
            finally:
                await safe_call_tool(
                    mcp_client,
                    "ha_remove_helpers_integrations",
                    {"target": entity_id, "confirm": True},
                )
                await _delete_leftover_backup(mcp_client, backup_name)
