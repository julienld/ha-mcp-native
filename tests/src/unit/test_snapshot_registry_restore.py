"""Scoped registry names remain intact in retained snapshot identities."""

from unittest.mock import AsyncMock


async def test_category_scope_containing_colon_is_not_split_into_another_scope(
    monkeypatch,
):
    from ha_mcp.backup_manager import _fetch_category
    from ha_mcp.tools import component_registries

    row = {"category_id": "stable", "name": "Category"}
    read = AsyncMock(return_value={"categories": {"custom:scope": [row]}})
    monkeypatch.setattr(component_registries, "fetch_registries_via_component", read)
    client = object()
    result = await _fetch_category(client, "custom:scope:stable")
    assert result == {"scope": "custom:scope", **row}
    read.assert_awaited_once_with(
        client, ["category"], category_scopes=["custom:scope"]
    )


async def test_area_restore_keeps_the_saved_floor_assignment():
    from types import SimpleNamespace

    from ha_mcp.backup_manager import _restore_area_or_floor

    before = {"area_id": "room", "name": "Room", "floor_id": "wrong-floor"}
    saved = {
        "kind": "area",
        "area_id": "room",
        "name": "Room",
        "floor_id": "saved-floor",
    }

    async def command(message):
        if message["type"].endswith("/list"):
            return {"success": True, "result": [before]}
        assert message["type"] == "config/area_registry/update"
        assert message["floor_id"] == "saved-floor"
        assert "kind" not in message
        return {
            "success": True,
            "result": {k: v for k, v in saved.items() if k != "kind"},
        }

    client = SimpleNamespace(send_websocket_message=AsyncMock(side_effect=command))
    result = await _restore_area_or_floor(client, "area:room", saved)
    assert result["floor_id"] == "saved-floor"
