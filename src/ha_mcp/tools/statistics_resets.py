"""Legacy recorder transport for the shared reset-timestamp recovery."""

from typing import Any

from ha_mcp.runtime.statistics_resets import _replace_resets as _replace_resets
from ha_mcp.runtime.statistics_resets import _reset_groups as _reset_groups
from ha_mcp.runtime.statistics_resets import restore_reset_timestamps as _restore_resets


async def restore_reset_timestamps(
    client: Any,
    rows_by_id: dict[str, Any],
    metadata: dict[str, dict[str, Any]],
    query: dict[str, Any],
    resolved_units: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    async def read(arguments: dict[str, Any]) -> dict[str, Any]:
        response = await client.send_websocket_message(
            {**arguments, "type": "recorder/statistics_during_period"}
        )
        if not response.get("success") or not isinstance(response.get("result"), dict):
            raise ValueError("Unconverted reset timestamp lookup failed")
        result: dict[str, Any] = response["result"]
        return result

    return await _restore_resets(read, rows_by_id, metadata, query, resolved_units)
