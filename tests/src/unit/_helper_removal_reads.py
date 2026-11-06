"""Stable storage images for tests focused on remote removal routing."""

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def stored_removal_images(monkeypatch: pytest.MonkeyPatch) -> None:
    """Routing fixtures own the WS reads; new before-image reads use this port."""

    async def read(
        _client: Any,
        _helper_type: str,
        item_id: str,
        _entity_id: str,
        name: str | None = None,
    ) -> dict[str, Any]:
        return {"success": True, "item_id": item_id, "item": {"id": item_id}}

    monkeypatch.setattr(
        "ha_mcp.tools.component_helper_collections.read_stored_helper_item", read
    )
