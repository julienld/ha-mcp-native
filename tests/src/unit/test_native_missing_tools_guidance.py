"""The native missing-entry refusal names the actual Home Assistant UI action."""

import pytest

from ha_mcp.tool_api import ToolFailure

from .test_native_engine_backend import native
from .test_native_file_access import files
from .test_native_filesystem import filesystem

__all__ = ["native", "files", "filesystem"]


async def test_missing_tools_entry_points_to_add_entry_without_writing(filesystem):
    service, _manager, owner, target = filesystem
    owner.entry = None
    with pytest.raises(ToolFailure) as missing:
        await service.write_file("www/notes.txt", "after", overwrite=True)
    assert missing.value.code == "COMPONENT_NOT_INSTALLED"
    assert "Add entry" in str(missing.value)
    assert "HA-MCP File & YAML Tools" in str(missing.value)
    assert missing.value.status == "not_started"
    assert target.read_text() == "before"
