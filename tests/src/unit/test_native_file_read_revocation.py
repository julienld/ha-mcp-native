"""An extra path removed during an awaited read cannot disclose its content."""

import pytest

from ha_mcp.tool_api import ToolFailure

from .test_native_engine_backend import native as native
from .test_native_file_access import files as files
from .test_native_filesystem import filesystem as filesystem


async def test_native_read_rechecks_the_path_permission_after_io(
    filesystem, monkeypatch
):
    service, manager, owner, target = filesystem
    import custom_components.ha_mcp_tools.file_access as module

    target.parent.rename(target.parent.with_name("extra"))
    owner.hass.data["ha_mcp_tools"]["allowed_paths"] = ["extra"]
    read = module._read_file_sync

    def revoke(path):
        result = read(path)
        owner.hass.data["ha_mcp_tools"]["allowed_paths"] = []
        return result

    monkeypatch.setattr(module, "_read_file_sync", revoke)
    with pytest.raises(ToolFailure, match="not allowed"):
        await service.read_file("extra/notes.txt")
