"""Qualify fixed native catalog URLs against real Core with its loopback guard."""

import asyncio
import json
from collections.abc import Callable
from typing import Any


async def exercise_catalogs(
    checks: Any, base: str, message: Callable[..., dict], unpack: Callable[..., dict]
) -> None:
    async def rpc(
        suffix: str, method: str, params: dict | None = None, *, modern: bool = True
    ) -> dict:
        response = await checks.client.post(
            base + checks.path + suffix,
            headers=checks.headers(
                method=method, name=(params or {}).get("name", ""), modern=modern
            ),
            json=message(method, params, modern=modern),
        )
        body = unpack(response)
        assert "error" not in body, body
        return body["result"]

    async def names(suffix: str) -> set[str]:
        return {tool["name"] for tool in (await rpc(suffix, "tools/list"))["tools"]}

    full, search, readonly, readonly_search, default = await asyncio.gather(
        *(
            names(suffix)
            for suffix in (
                "/catalog/full",
                "/catalog/search",
                "/readonly/catalog/full",
                "/readonly/catalog/search",
                "",
            )
        )
    )
    assert full == default and "ha_get_state" in full, full
    assert "ha_search_tools" not in full and "ha_search_tools" in search, search
    assert "ha_get_state" not in search and "ha_get_state" in readonly, readonly
    assert (
        "ha_call_write_tool" in search and "ha_call_write_tool" not in readonly_search
    )
    assert "ha_call_delete_tool" not in readonly_search, readonly_search
    checks.passed(
        "native_catalog_URLs_keep_concurrent_full_search_and_readonly_isolated"
    )

    instructions = {}
    for suffix in ("", "/catalog/full", "/catalog/search"):
        discover = await rpc(suffix, "server/discover")
        legacy = await rpc(
            suffix,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "native-catalog-ci", "version": "1"},
            },
            modern=False,
        )
        assert discover["instructions"] == legacy["instructions"], suffix
        instructions[suffix] = discover["instructions"]
    assert instructions[""] == instructions["/catalog/full"]
    assert instructions["/catalog/search"] != instructions["/catalog/full"]
    assert "ha_search_tools" in instructions["/catalog/search"]
    checks.passed(
        "native_catalog_discover_and_initialize_match_URL_with_search_default_off"
    )

    session = await checks.client.post(
        base + "/api/ha_mcp_tools/ui/session", headers=checks.headers()
    )
    session.raise_for_status()
    tools_url = base + "/api/ha_mcp_tools/ui/app/api/settings/tools"
    before = await checks.client.get(tools_url)
    before.raise_for_status()
    original = before.json()
    hidden_name = "ha_list_services"
    assert hidden_name in full, full
    changed = await checks.client.post(
        tools_url, json={"states": {hidden_name: "disabled"}}
    )
    changed.raise_for_status()
    try:
        visible, found = await asyncio.gather(
            names("/catalog/full"),
            rpc(
                "/catalog/search",
                "tools/call",
                {
                    "name": "ha_search_tools",
                    "arguments": {"tools": [hidden_name]},
                },
            ),
        )
        assert hidden_name not in visible and found.get("isError"), found
        checks.passed("native_catalog_hidden_tool_stays_hidden_in_search_and_full")
    finally:
        restored = await checks.client.post(
            tools_url,
            json={
                "states": original["states"],
                "llm_api": original["llm_api_overrides"],
            },
        )
        restored.raise_for_status()

    await _readonly_calls(checks, rpc)
    assert await names("/catalog/full") == full
    for suffix in ("/catalog/full", "/catalog/search"):
        denied = await checks.client.post(
            base + checks.path + suffix,
            headers=checks.headers("reader"),
            json=message("tools/list"),
        )
        assert denied.status_code in (401, 403), denied.status_code
    probe = await checks.control("probe")
    assert probe["loopback_attempts"] == 0, probe
    checks.passed(
        "native_catalog_readonly_refusals_and_auth_keep_Core_unchanged_without_loopback"
    )


async def _readonly_calls(checks: Any, rpc: Callable[..., Any]) -> None:
    baseline = await checks.call(
        "ha_get_state", {"entity_id": "input_boolean.native_light"}
    )
    for suffix, name, arguments in (
        (
            "/readonly/catalog/full",
            "ha_call_service",
            {
                "domain": "input_boolean",
                "service": "toggle",
                "entity_id": "input_boolean.native_light",
            },
        ),
        (
            "/readonly/catalog/search",
            "ha_call_write_tool",
            {
                "name": "ha_call_service",
                "arguments": {
                    "domain": "input_boolean",
                    "service": "toggle",
                    "entity_id": "input_boolean.native_light",
                },
            },
        ),
    ):
        refused = await rpc(
            suffix, "tools/call", {"name": name, "arguments": arguments}
        )
        assert refused.get("isError"), refused
        code = "READ_ONLY" if name == "ha_call_service" else "RESOURCE_NOT_FOUND"
        assert code in json.dumps(refused), refused
    after = await checks.call(
        "ha_get_state", {"entity_id": "input_boolean.native_light"}
    )
    assert after["data"]["state"] == baseline["data"]["state"], after
