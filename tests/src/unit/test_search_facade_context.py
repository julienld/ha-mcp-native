"""Retained search resolves progress from each real invocation context."""

import asyncio
from unittest.mock import AsyncMock

from ha_mcp.tools import tools_search


async def test_search_progress_context_is_resolved_per_call_without_instance_state(
    monkeypatch,
):
    from ha_mcp._vendor.fastmcp.server import dependencies

    first, second = object(), object()
    contexts = iter([first, second])
    monkeypatch.setattr(dependencies, "get_context", lambda: next(contexts))
    monkeypatch.setattr(
        tools_search, "_component_serves_search_types", lambda req: False
    )
    owner = tools_search.SearchTools(object(), object())
    owner._legacy_ha_search = AsyncMock(return_value={"success": True})
    results = await asyncio.gather(
        owner.ha_search(query="first"), owner.ha_search(query="second")
    )
    assert all(result["success"] for result in results)
    assert [call.args[1] for call in owner._legacy_ha_search.await_args_list] == [
        first,
        second,
    ]
    assert [call.args[0].query for call in owner._legacy_ha_search.await_args_list] == [
        "first",
        "second",
    ]
