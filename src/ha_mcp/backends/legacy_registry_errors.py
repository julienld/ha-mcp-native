"""Legacy MCP error facade shared by entity metadata and exposure reads."""

from typing import Any, NoReturn


def raise_entity_read_error(error: Exception, entity_id: Any) -> NoReturn:
    from ha_mcp._vendor.fastmcp.exceptions import ToolError
    from ha_mcp.tools.helpers import exception_to_structured_error

    if isinstance(error, ToolError):
        raise error
    exception_to_structured_error(error, context={"entity_id": entity_id})
