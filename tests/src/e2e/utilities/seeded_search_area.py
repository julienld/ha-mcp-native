"""Prepare legal multi-domain search seeds and restore their registry choices."""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from .assertions import MCPAssertions
from .wait_helpers import wait_for_tool_result


@asynccontextmanager
async def seeded_search_area(
    mcp_client: Any, entity_ids: Sequence[str], area_id: str
) -> AsyncIterator[dict[str, Any]]:
    """Assign an explicit name/area, preserving exact state even after a failure."""
    saved = {}
    async with MCPAssertions(mcp_client) as mcp:
        try:
            for eid in entity_ids:
                entry = (
                    await mcp.call_tool_success("ha_get_entity", {"entity_id": eid})
                )["entity_entry"]
                # Record intent before dispatch: even a failed write may have
                # changed state. Core 2026.10 disallows an entity area when a
                # has_entity_name entity has no name of its own (the HAOS demos
                # use their device name). Supply one instead of skipping tests.
                saved[eid] = {
                    "name": entry.get("name"),
                    "area_id": entry.get("area_id"),
                }
                await mcp.call_tool_success(
                    "ha_set_entity",
                    {
                        "entity_id": eid,
                        "name": f"E2E search {eid}",
                        "area_id": area_id,
                    },
                )
            # Poll only the read path until all assigned entities are visible.
            expected = set(entity_ids)
            await wait_for_tool_result(
                mcp_client,
                tool_name="ha_search",
                arguments={"area_filter": area_id, "limit": 50},
                predicate=lambda d: expected.issubset(
                    {r.get("entity_id") for r in d.get("data", d).get("entities", [])}
                ),
                description=f"all seed assignments visible under {area_id}",
            )
            yield {
                "area_id": area_id,
                "entity_ids": list(entity_ids),
            }
        finally:
            cleanup_errors = []
            for eid, before in reversed(saved.items()):
                try:
                    # name='' means reset-to-default; the distinct stored ''
                    # override is restored via Core's use-device-name switch.
                    name = (
                        {"use_device_name": True}
                        if before["name"] == ""
                        else {"name": before["name"] or ""}
                    )
                    await mcp.call_tool_success(
                        "ha_set_entity",
                        {
                            "entity_id": eid,
                            "area_id": before["area_id"] or "",
                            **name,
                        },
                    )
                    await wait_for_tool_result(
                        mcp_client,
                        tool_name="ha_get_entity",
                        arguments={"entity_id": eid},
                        predicate=lambda data, expected=before: all(
                            data.get("entity_entry", {}).get(key) == value
                            for key, value in expected.items()
                        ),
                        description=f"original name and area restored for {eid}",
                    )
                except Exception as exc:  # noqa: BLE001 - finish restoring other owned changes
                    cleanup_errors.append(exc)
            if cleanup_errors:
                raise ExceptionGroup(
                    "Seeded search fixture cleanup failed", cleanup_errors
                )
