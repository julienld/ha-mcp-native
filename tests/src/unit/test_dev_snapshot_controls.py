"""An agent cannot enable snapshot deletion or lower its human-set age floor."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError


@pytest.mark.parametrize(
    "field,value",
    [("enable_snapshot_delete", True), ("snapshot_delete_min_age_days", 0)],
)
async def test_legacy_developer_rejects_snapshot_delete_controls_before_persist(
    monkeypatch, field, value
):
    from ha_mcp.settings_ui import _handlers_backups
    from ha_mcp.tools.dev_backup import apply_dev_backup_config

    persisted = AsyncMock(
        return_value=SimpleNamespace(body=b'{"success": true}', status_code=200)
    )
    monkeypatch.setattr(_handlers_backups, "apply_backup_config", persisted)
    with pytest.raises(ToolError, match="human-only"):
        await apply_dev_backup_config(None, {field: value, "enable_auto_backup": False})
    persisted.assert_not_called()
