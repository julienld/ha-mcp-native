"""Use the validation and tool-result shapes supplied by the running Core."""

import importlib
from typing import Any

from homeassistant.helpers import llm

# Core's base tool owns the schema generation; do not add a new schema package
# to an older Core just to make the qualification fixture importable.
validation = importlib.import_module(type(llm.Tool.parameters).__module__.split(".")[0])


def tool_result(data: dict[str, Any]) -> Any:
    result_type = getattr(llm, "ToolResult", None)
    return data if result_type is None else result_type(data=data)


def result_payload(result: Any) -> dict[str, Any]:
    """Present the same observation while preserving the legacy error markers."""
    if not isinstance(result, dict):
        return {"data": result.data, "error": result.error}
    return {
        "data": result,
        "error": bool(
            result.get("isError")
            or result.get("error")
            or result.get("success") is False
        ),
    }
