"""Live functional contracts, kept outside the timing loop."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from .wire import VERSION, Client, Observation, WireError, tool_payload

ENTITY = "sensor.protocol_fixture"
READS = (
    ("ha_get_state", {"entity_id": ENTITY}),
    ("ha_eval_template", {"template": "{{ states('sensor.protocol_fixture') }}"}),
    ("ha_list_services", {"domain": "protocol_fixture"}),
)


def validate_read(index: int, observation: Observation, state: str = "ready") -> None:
    payload = tool_payload(observation)
    if index == 0:
        valid = payload.get("data", {}).get("state") == state
    elif index == 1:
        valid = payload.get("result") == state
    else:
        # Tool versions project service maps differently; require both actual keys.
        valid = _has_key(payload, "protocol_fixture") and _has_key(payload, "echo")
    if not valid:
        raise WireError("read_semantics_mismatch")


def _has_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_has_key(child, key) for child in value.values())
    if isinstance(value, list):
        return any(_has_key(child, key) for child in value)
    return isinstance(value, str) and value == key


async def tool_catalog(client: Client) -> dict[str, Any]:
    tools: dict[str, Any] = {}
    cursor = None
    for _ in range(32):
        result = (
            await client.rpc("tools/list", {"cursor": cursor} if cursor else {})
        ).result()
        for tool in result["tools"]:
            if tool["name"] in tools:
                raise WireError("duplicate_tool_name")
            tools[tool["name"]] = tool
        cursor = result.get("nextCursor")
        if not cursor:
            return tools
    raise WireError("catalog_page_limit")


async def protocol_contract(client: Client) -> dict[str, Any]:
    discovery = await client.rpc("server/discover")
    if VERSION not in discovery.result().get("supportedVersions", []):
        raise WireError("modern_version_not_advertised")
    tools = await tool_catalog(client)
    for name, _ in READS:
        if tools[name].get("annotations", {}).get("readOnlyHint") is not True:
            raise WireError("read_annotation_missing")
        if _has_key(tools[name].get("inputSchema", {}), "x-mcp-header"):
            raise WireError("custom_parameter_headers_not_supported")
    if tools["ha_call_service"].get("annotations", {}).get("readOnlyHint") is True:
        raise WireError("write_annotated_as_read")
    invalid = await client.rpc("server/discover", version="2099-01-01")
    if (
        invalid.status != 400
        or (invalid.reply or {}).get("error", {}).get("code") != -32022
    ):
        raise WireError("invalid_version_not_explicitly_rejected")
    legacy = await client.rpc(
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "bounded-legacy-probe", "version": "1"},
        },
        legacy=True,
        version="2025-11-25",
    )
    legacy_version = legacy.result().get("protocolVersion")
    if legacy_version != "2025-11-25":
        raise WireError("legacy_initialize_version_mismatch")
    legacy_tools = await client.rpc("tools/list", legacy=True, version=legacy_version)
    if not any(
        tool["name"] == "ha_get_state"
        for tool in legacy_tools.result().get("tools", [])
    ):
        raise WireError("legacy_catalog_missing_read")
    legacy_read = await client.rpc(
        "tools/call",
        {
            "name": "ha_get_state",
            "arguments": {"entity_id": ENTITY},
        },
        legacy=True,
        version=legacy_version,
    )
    validate_read(0, legacy_read)
    return {
        "modern_discovery": discovery.public(),
        "tools_count": len(tools),
        "read_only_annotations": True,
        "write_annotation_not_read_only": True,
        "invalid_version": invalid.public(),
        "legacy_initialize": legacy.public(),
        "legacy_read": legacy_read.public(),
        "legacy_protocol": legacy_version,
    }


async def service_call(client: Client, marker: str) -> Observation:
    return await client.rpc(
        "tools/call",
        {
            "name": "ha_call_service",
            "arguments": {
                "domain": "protocol_fixture",
                "service": "echo",
                "data": {"marker": marker},
                "return_response": True,
            },
        },
    )


def _same_json(actual: Any, expected: Any) -> bool:
    """Compare bounded wire JSON without Python's bool/int equality coercion."""
    return json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)


async def actor_contract(target: Any) -> dict[str, Any]:
    observed = []
    for index, actor in enumerate(target.actors):
        before = await target.snapshot()
        marker = ("actor-a", "actor-b")[index]
        result = tool_payload(await service_call(actor["client"], marker))
        expected = {
            "marker": marker,
            "actor": actor["expected_user_id"],
            "nested": {"values": [0, False, ""], "empty": {}},
        }
        if not _same_json(result.get("service_response"), expected):
            raise WireError("service_response_or_actor_mismatch")
        snapshot = await target.snapshot()
        if (
            not _same_json(snapshot["records"], before["records"] + [expected])
            or snapshot["state"] != marker
        ):
            raise WireError("core_effect_or_actor_mismatch")
        observed.append(snapshot["records"][-1]["actor"])
    return {
        "return_response_true": True,
        "core_effects_verified": True,
        "actor_matches_expected": True,
        "distinct_actors_observed": observed[0] != observed[1],
    }


async def read_only_contract(target: Any) -> dict[str, Any]:
    before = await target.snapshot()
    try:
        await target.policy(True)
        await target.wait_ready()
        client = target.actors[0]["client"]
        tools = await tool_catalog(client)
        response = await service_call(client, "blocked")
        rejected = (
            "error" in (response.reply or {})
            or (response.reply or {}).get("result", {}).get("isError") is True
        )
        if response.failure or response.status >= 500 or not rejected:
            raise WireError("read_only_write_not_explicitly_rejected")
        if "ha_call_service" in tools and not re.search(
            r"read.only|permission|disabled|blocked",
            json.dumps(response.reply),
            re.IGNORECASE,
        ):
            raise WireError("write_failure_not_a_policy_denial")
        read = await client.rpc(
            "tools/call", {"name": READS[0][0], "arguments": READS[0][1]}
        )
        validate_read(0, read, before["state"])
        after = await target.snapshot()
        if after["state"] != before["state"] or not _same_json(
            after["records"], before["records"]
        ):
            raise WireError("read_only_call_changed_fixture")
        result = {
            "write_rejected": True,
            "read_succeeded": True,
            "no_service_effect": True,
            "write_visible_in_catalog": "ha_call_service" in tools,
            "rejection": response.public(),
        }
    finally:
        await target.policy(False)
        await target.wait_ready()
    client = target.actors[0]["client"]
    restored = tool_payload(await service_call(client, "actor-a"))
    expected = {
        "marker": "actor-a",
        "actor": target.actors[0]["expected_user_id"],
        "nested": {"values": [0, False, ""], "empty": {}},
    }
    after_restore = await target.snapshot()
    if (
        not _same_json(restored.get("service_response"), expected)
        or after_restore["state"] != "actor-a"
        or not _same_json(after_restore["records"], before["records"] + [expected])
    ):
        raise WireError("read_only_restoration_not_effective")
    return {**result, "writes_work_after_restoration": True}


async def ready(target: Any) -> None:
    async with asyncio.timeout(120):
        while True:
            if (await target.snapshot())["running"]:
                probe = await target.actors[0]["client"].rpc("server/discover")
                if (
                    probe.status == 200
                    and not probe.failure
                    and "result" in (probe.reply or {})
                ):
                    return
            await asyncio.sleep(0.5)
