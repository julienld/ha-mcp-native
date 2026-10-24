"""Public MCP proof that registry history restores the entity's hidden setting."""

from typing import Any

from .assertions import MCPAssertions
from .wait_helpers import wait_for_tool_result

_LIST = {"scope": "edits", "action": "list", "domain": "entity_registry"}


def _names(data: dict[str, Any]) -> set[str]:
    return {row["name"] for row in data["data"]["backups"]}


async def assert_entity_registry_backup(mcp_client: Any, entity_id: str) -> None:
    """Require an auto-capture, a useful restore and the matching safety image."""
    async with MCPAssertions(mcp_client) as mcp:
        before = (
            await mcp.call_tool_success("ha_get_entity", {"entity_id": entity_id})
        )["entity_entry"]
        # The demo entity supports these public writable visibility values.
        # An unexpected seed shape is a setup failure, never a reason to skip.
        assert before["hidden_by"] in (None, "user"), before
        existing = _names(await mcp.call_tool_success("ha_manage_backup", _LIST))
        changed = {
            **before,
            "hidden": not before["hidden"],
            "hidden_by": None if before["hidden"] else "user",
        }
        try:
            await mcp.call_tool_success(
                "ha_set_entity", {"entity_id": entity_id, "hidden": changed["hidden"]}
            )
            await _wait_for_entry(mcp_client, entity_id, changed, "edited visibility")
            listing = await wait_for_tool_result(
                mcp_client,
                tool_name="ha_manage_backup",
                arguments=_LIST,
                predicate=lambda data: bool(_names(data) - existing),
                description="new entity-registry pre-edit capture",
            )
            created = _names(listing) - existing
            assert len(created) == 1, listing
            backup_name = created.pop()
            snapshot = await _view(mcp, backup_name)
            assert snapshot["domain"] == "entity_registry", snapshot
            assert snapshot["entity_id"] == snapshot["config"]["id"], snapshot
            assert snapshot["config"]["entity_id"] == entity_id, snapshot
            assert snapshot["config"]["hidden_by"] == before["hidden_by"], snapshot

            restored = await mcp.call_tool_success(
                "ha_manage_backup",
                {"scope": "edits", "action": "restore", "backup_name": backup_name},
            )
            safety_name = restored["data"]["safety_backup"]
            assert isinstance(safety_name, str) and safety_name != backup_name, restored
            await _wait_for_entry(mcp_client, entity_id, before, "restored registry")
            safety = await _view(mcp, safety_name)
            assert safety["domain"] == "entity_registry", safety
            assert safety["entity_id"] == snapshot["entity_id"], safety
            assert safety["config"]["hidden_by"] == changed["hidden_by"], safety
        finally:
            # Read before recovery: do not replay the edit or restore. If the
            # assertion failed after an effect, explicitly recover the original
            # setting once, then poll only reads. Cleanup errors fail the case.
            errors = []
            try:
                current = await mcp.call_tool_success(
                    "ha_get_entity", {"entity_id": entity_id}
                )
                if current["entity_entry"]["hidden_by"] != before["hidden_by"]:
                    await mcp.call_tool_success(
                        "ha_set_entity",
                        {"entity_id": entity_id, "hidden": before["hidden"]},
                    )
                await _wait_for_entry(mcp_client, entity_id, before, "cleanup registry")
            except Exception as error:  # noqa: BLE001 - still remove owned history
                errors.append(error)
            try:
                owned = (
                    _names(await mcp.call_tool_success("ha_manage_backup", _LIST))
                    - existing
                )
                for name in sorted(owned):
                    try:
                        await mcp.call_tool_success(
                            "ha_manage_backup",
                            {"scope": "edits", "action": "delete", "backup_name": name},
                        )
                    except Exception as error:  # noqa: BLE001 - attempt remaining owned records
                        errors.append(error)
            except Exception as error:  # noqa: BLE001 - report collection failure with restoration
                errors.append(error)
            if errors:
                raise ExceptionGroup("Entity-registry backup cleanup failed", errors)


async def _view(mcp: MCPAssertions, backup_name: str) -> dict[str, Any]:
    result = await mcp.call_tool_success(
        "ha_manage_backup",
        {"scope": "edits", "action": "view", "backup_name": backup_name},
    )
    return result["data"]


async def _wait_for_entry(
    client: Any, entity_id: str, expected: dict[str, Any], description: str
) -> None:
    await wait_for_tool_result(
        client,
        tool_name="ha_get_entity",
        arguments={"entity_id": entity_id},
        predicate=lambda data: data.get("entity_entry") == expected,
        description=description,
    )
