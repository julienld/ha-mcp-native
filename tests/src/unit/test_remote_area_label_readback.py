"""An area-label acknowledgement without labels still permits a verified readback."""

from unittest.mock import AsyncMock

import pytest

from ha_mcp.backends.remote import RemoteBackend
from ha_mcp.runtime.area_config import AreaConfigTools
from ha_mcp.tool_api import CallContext
from ha_mcp.tools.tools_areas import AreaTools


async def test_remote_area_labels_are_verified_after_an_incomplete_write_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch = AsyncMock(return_value=([{"area_id": "kitchen", "labels": ["label"]}], []))
    monkeypatch.setattr(AreaTools, "_fetch_area_floor_registries", fetch)
    tools = AreaConfigTools(RemoteBackend(object()), CallContext("actor"))
    await tools._verify_area_labels_written(
        parsed_labels=["label"],
        kind="area",
        data={"area_id": "kitchen"},
        returned_id="kitchen",
        operation="update",
        name=None,
    )
    fetch.assert_awaited_once()
