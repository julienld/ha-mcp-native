"""Public-tool assertions against a real HACS owner and explicit network fixtures."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
import httpx

REPOSITORY = "qualification/hacs-owner-fixture"
REPOSITORY_ID = "987654321"
Control = Callable[..., Awaitable[dict[str, Any]]]


async def hacs_call(
    checks: Any, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    result = await checks.call(name, arguments)
    # The shared HACS facade wraps every successful action in timezone metadata.
    # Keep the actual response in the log; never accept a flattened/incomplete
    # substitute or unwrap the repository's own nested data a second time.
    print(
        "HACS_TOOL_RESULT "
        + json.dumps({"tool": name, "arguments": arguments, "result": result}),
        flush=True,
    )
    assert result.get("success") is True, result
    assert isinstance(result.get("metadata"), dict), result
    assert result["metadata"].get("home_assistant_timezone"), result
    assert isinstance(result.get("data"), dict), result
    return result["data"]


def payload(body: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in body, body
    result = body["result"]
    return result.get("structuredContent") or json.loads(
        next(block["text"] for block in result["content"] if block["type"] == "text")
    )


async def wait_probe(
    control: Control, predicate: Callable[[dict[str, Any]], bool]
) -> dict[str, Any]:
    async with asyncio.timeout(120):
        while True:
            observed = await control("probe")
            if predicate(observed):
                return observed
            await asyncio.sleep(0.25)


async def wait_native_entry(checks: Any) -> None:
    """HTTP/auth can return before the test integration registers its view."""
    async with asyncio.timeout(90):
        while True:
            try:
                await checks.ready()
                return
            except httpx.HTTPStatusError as error:
                if error.response.status_code != 404:
                    raise
            await asyncio.sleep(0.5)


def unchanged(before: dict[str, Any], after: dict[str, Any]) -> None:
    for key in ("repository", "disk", "network", "install_services"):
        assert after[key] == before[key], (key, before[key], after[key])


async def ws_cache(base: str, token: str) -> None:
    async with (
        aiohttp.ClientSession(trust_env=False) as session,
        session.ws_connect(base + "/api/websocket") as websocket,
    ):
        assert (await websocket.receive_json())["type"] == "auth_required"
        await websocket.send_json({"type": "auth", "access_token": token})
        assert (await websocket.receive_json())["type"] == "auth_ok"
        for index, command in enumerate(
            (
                {"type": "ha_mcp_tools/info"},
                {"type": "ha_mcp_tools/hacs_cache", "repository_id": REPOSITORY_ID},
            ),
            1,
        ):
            await websocket.send_json({"id": index, **command})
            response = await websocket.receive_json()
            assert response["success"], response
            if index == 1:
                assert "hacs_cache" in response["result"]["capabilities"], response
            else:
                cached = response["result"]
                assert cached["store"] == "hacs", cached
                assert cached["repository"]["id"] == REPOSITORY_ID, cached
                assert cached["repository"]["full_name"] == REPOSITORY, cached
                assert cached["cache"]["refreshed_from_github"] is False, cached


async def exercise_admission(checks: Any, control: Control, base: str) -> None:
    owner = await wait_probe(control, lambda p: p["ready"])
    assert owner["core"] == "2026.10.4" and owner["hacs"] == "2.0.5", owner
    assert owner["owner_class"] == "custom_components.hacs.base.HacsBase", owner
    assert owner["entry_state"] == "loaded", owner
    checks.passed("real_Core_2026_10_4_HACS_2_0_5_loaded_owner")

    # Register through the production tool, with real HACS validators and GitHub
    # client models populated by HTTP fixture payloads.
    added = await hacs_call(
        checks,
        "ha_manage_hacs",
        {
            "action": "add_repository",
            "repository": REPOSITORY,
            "category": "integration",
        },
    )
    assert added["repository_id"] == REPOSITORY_ID, added
    assert added["repository"] == REPOSITORY, added
    assert added["category"] == "integration", added
    checks.passed("real_HACS_registration_from_controlled_GitHub_metadata")
    before = await control("probe")
    assert before["repository"]["data"]["new"] is True, before
    assert before["update_entity"] is None, before
    search = await hacs_call(
        checks, "ha_get_hacs_info", {"action": "search", "query": REPOSITORY}
    )
    assert search["count"] == 1, search
    assert len(search["results"]) == 1, search
    assert search["results"][0]["id"] == REPOSITORY_ID, search
    assert search["results"][0]["full_name"] == REPOSITORY, search
    assert search["cache"]["refreshed_from_github"] is False, search
    info = await hacs_call(
        checks, "ha_get_hacs_info", {"action": "info", "repository_id": REPOSITORY_ID}
    )
    assert info["repository_id"] == REPOSITORY_ID, info
    assert info["full_name"] == REPOSITORY, info
    assert info["cache"]["refreshed_from_github"] is False, info
    await ws_cache(base, checks.credentials["alpha"]["access_token"])
    unchanged(before, await control("probe"))
    checks.passed(
        "MCP_and_shared_WS_cache_reads_preserve_new_metadata_disk_and_network"
    )

    response = await checks.client.post(
        base + checks.path,
        headers=checks.headers("reader", method="tools/call", name="ha_manage_hacs"),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "ha_manage_hacs",
                "arguments": {"action": "download", "repository_id": REPOSITORY_ID},
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        },
    )
    assert response.status_code in (401, 403), response.status_code
    unchanged(before, await control("probe"))
    checks.passed("non_admin_rejected_before_any_owner_network_or_file_effect")

    # A dummy must never qualify the new self-updater as an HA-MCP owner.
    guide = await checks.call("ha_manage_ha_mcp_updates", {"action": "check"})
    assert guide["capability"] == "manual_required", guide
    failed = await checks.rpc(
        "tools/call",
        {"name": "ha_manage_ha_mcp_updates", "arguments": {"action": "update"}},
    )
    assert failed["result"].get("isError"), failed
    assert payload(failed)["error"]["code"] == "UNSUPPORTED_OPERATION", failed
    unchanged(before, await control("probe"))
    checks.passed("dummy_repository_cannot_authorize_ha_mcp_self_update")


async def exercise_hacs_owner(
    checks: Any, control: Control, *, base: str, admin_id: str
) -> dict[str, Any]:
    await exercise_admission(checks, control, base)

    async def install(version: str) -> dict[str, Any]:
        result = await hacs_call(
            checks,
            "ha_manage_hacs",
            {
                "action": "download",
                "repository_id": REPOSITORY_ID,
                "version": version,
            },
        )
        assert result["repository_id"] == REPOSITORY_ID, result
        assert result["repository"] == REPOSITORY, result
        assert result["installed_version"] == version, result
        assert result["restart_requested"] is False, result
        assert result["active_version_verified"] is False, result
        return result

    installed = await install("1.0.0")
    assert installed["installed_version"] == "1.0.0", installed
    after_first = await wait_probe(
        control, lambda p: bool(p["update_entity"] and p["update_state"])
    )
    assert after_first["update_state"]["attributes"]["installed_version"] == "1.0.0", (
        after_first
    )
    assert not after_first["install_services"], after_first
    await control("load_dummy", {})
    assert (await control("probe"))["dummy_running_version"] == "1.0.0"
    checks.passed(
        "first_install_real_repository_files_and_owner_update_entity_creation"
    )

    updated = await install("2.0.0")
    after_update = await control("probe")
    assert updated["installed_version"] == "2.0.0" and updated["pending_restart"], (
        updated
    )
    assert updated["restart_requested"] is False, updated
    assert after_update["dummy_running_version"] == "1.0.0", after_update
    assert len(after_update["install_services"]) == 1, after_update
    dispatch = after_update["install_services"][0]
    assert dispatch["user_id"] == admin_id, dispatch
    assert dispatch["data"]["version"] == "2.0.0", dispatch
    assert after_update["update_entity"] in dispatch["data"]["entity_id"], dispatch
    checks.passed(
        "real_Core_update_install_forwards_admin_context_and_keeps_loaded_old_code"
    )
    return await exercise_recovery(checks, control, install, after_update)


async def exercise_recovery(
    checks: Any,
    control: Control,
    install: Callable[[str], Awaitable[dict[str, Any]]],
    after_update: dict[str, Any],
) -> dict[str, Any]:

    # Force HACS's own backup/restore after partially extracting a corrupt ZIP.
    await control("archive_fault", {"corrupt": True})
    failed = await checks.rpc(
        "tools/call",
        {
            "name": "ha_manage_hacs",
            "arguments": {
                "action": "download",
                "repository_id": REPOSITORY_ID,
                "version": "3.0.0",
            },
        },
    )
    assert failed["result"].get("isError"), failed
    assert payload(failed)["error"]["code"] == "OUTCOME_UNKNOWN", failed
    after_failure = await control("probe")
    assert after_failure["archive_entered"], after_failure
    assert not after_failure["unexpected_network"], after_failure
    archive_requests = after_failure["network"][len(after_update["network"]) :]
    assert (
        sum(request.endswith("/3.0.0/fixture.zip") for request in archive_requests) == 1
    ), archive_requests

    def dummy_files(observation: dict[str, Any]) -> dict[str, str]:
        return {
            k: v
            for k, v in observation["disk"].items()
            if k.startswith("custom_components/")
        }

    assert dummy_files(after_update), after_update
    assert dummy_files(after_failure) == dummy_files(after_update), after_failure
    assert after_failure["repository"]["data"]["installed_version"] == "2.0.0", (
        after_failure
    )
    assert len(after_failure["install_services"]) == 2, after_failure
    checks.passed(
        "corrupt_second_ZIP_member_triggers_real_HACS_code_rollback_without_retry"
    )

    await control("archive_fault", {})
    recovered = await install("3.0.0")
    assert recovered["installed_version"] == "3.0.0", recovered
    checks.passed("explicit_retry_after_inspection_recovers_from_failed_owner_install")

    # Abandon only the response while the real archive request is in flight.
    # The next action is observation, never automatic replay.
    await control("archive_fault", {"hold": True})
    lost = asyncio.create_task(install("2.0.0"))
    await wait_probe(control, lambda p: p["archive_entered"])
    lost.cancel()
    try:
        await lost
    except (asyncio.CancelledError, httpx.ReadError):
        pass
    await control("release_archive", {})
    after_lost = await wait_probe(
        control,
        lambda p: (
            p["repository"]["data"]["installed_version"] == "2.0.0"
            and not p["update_state"]["attributes"].get("in_progress")
        ),
    )
    # The version changes before the adapter's durable storage write completes.
    # Wait for its owned task before reloading the owner; never race the receipt.
    await wait_probe(checks.control, lambda p: not p["owned_tasks"])
    assert len(after_lost["install_services"]) == 4, after_lost
    checks.passed(
        "lost_MCP_response_observed_once_without_replaying_real_owner_install"
    )

    await control("reload_owner", {})
    restored = await wait_probe(control, lambda p: p["ready"])
    assert restored["repository"]["data"]["installed_version"] == "2.0.0", restored
    # HACS reload clears its pending_restart flag even though Python still has
    # the old module. Record that owner limitation rather than calling it active.
    assert restored["dummy_running_version"] == "1.0.0", restored
    checks.passed(
        "HACS_storage_restore_preserves_installed_version_while_loaded_code_stays_old"
    )
    assert not restored["unexpected_network"], restored["unexpected_network"]
    return restored
