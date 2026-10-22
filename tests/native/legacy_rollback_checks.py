"""Qualify the retained legacy runtime with the exact native wheel and ZIP.

Run after native interop, then run the real Core process-restart check before
the final native import/listener probe. A runtime switch does not erase cached
legacy imports. This scenario qualifies Core 2026.10.2, not legacy Core 2026.8.
"""

import asyncio
import time
from typing import Any

from camera_checks import _camera
from oauth_checks import _code, _exchange, _refresh, _register

_OAUTH = "http://127.0.0.1:8123/api/ha_mcp_tools/oauth"


async def _ready(checks: Any, phase: str) -> dict[str, Any]:
    deadline = time.monotonic() + 180
    while True:
        status: dict[str, Any] = await checks.control(
            "legacy_rollback", {"action": "status"}
        )
        if status["phase"] == phase and status["ready"]:
            return status
        assert time.monotonic() < deadline, (
            f"Rollback runtime did not become ready: {phase}"
        )
        await asyncio.sleep(0.5)


def _preserved(status: dict[str, Any]) -> None:
    for key in (
        "entry_preserved",
        "webhook_preserved",
        "data_preserved",
        "artifacts_unchanged",
        "llm_ids_preserved",
        "state_preserved",
    ):
        assert status[key], f"Runtime switch lost {key}"


async def _native_image(checks: Any) -> None:
    camera = await _camera(checks)
    reply = await checks.rpc(
        "tools/call",
        {
            "name": "ha_get_camera_image",
            "arguments": {"entity_id": camera["entity_id"]},
        },
    )
    assert "error" not in reply, "Native MCP dispatch failed after legacy teardown"
    result = reply["result"]
    assert not result.get("isError"), "Native image failed after legacy teardown"
    assert any(block["type"] == "image" for block in result["content"]), (
        "Native rich ToolResult was lost after switching runtimes"
    )
    assert result["_meta"]["ha_mcp"]["status"] == "confirmed", (
        "Native operation outcome changed after switching runtimes"
    )


async def _cleanup_rollback(
    checks: Any, envelope: str, *, restore: bool, primary: BaseException | None
) -> None:
    failures: list[tuple[str, Exception]] = []
    if restore:
        try:
            await checks.control("legacy_rollback", {"action": "return"})
        except Exception as error:  # noqa: BLE001 - collected and reported below
            failures.append(("native return", error))
    try:
        revoked = await checks.client.post(_OAUTH + "/revoke", data={"token": envelope})
        assert revoked.status_code == 200, (
            f"Rollback test grant cleanup failed (HTTP {revoked.status_code})"
        )
    except Exception as error:  # noqa: BLE001 - collected and reported below
        failures.append(("OAuth grant", error))
    if primary is not None:
        for stage, cleanup_error in failures:
            primary.add_note(
                f"Rollback {stage} cleanup also failed: {type(cleanup_error).__name__}"
            )
    elif failures:
        raise ExceptionGroup(
            "Rollback cleanup failed", [error for _, error in failures]
        )


async def exercise_legacy_rollback(checks: Any) -> None:
    path = checks.path
    before = await checks.call(
        "ha_get_state", {"entity_id": "input_boolean.native_light"}
    )
    state = before["data"]["state"]
    client_id = await _register(checks)
    code, verifier = await _code(checks, client_id)
    response = await _exchange(checks, client_id, code, verifier)
    assert response.status_code == 200, "Rollback grant preparation failed"
    envelope = response.json()["refresh_token"]
    started = False
    returned = False
    primary: BaseException | None = None
    try:
        started = True
        await checks.control("legacy_rollback", {"action": "start"})
        legacy = await _ready(checks, "legacy")
        _preserved(legacy)
        assert legacy["legacy_thread"] and legacy["legacy_listener"], (
            "Legacy readiness did not establish its actual worker/listener"
        )
        assert not legacy["guard_enabled"], "Legacy loopback contract was not enabled"
        legacy_read = await checks.call(
            "ha_get_state", {"entity_id": "input_boolean.native_light"}
        )
        assert legacy_read["data"]["state"] == state, (
            "Legacy changed or lost the real Core state"
        )
        assisted = await checks.control("legacy_rollback", {"action": "assist"})
        assert not assisted["error"], "Legacy Assist returned a tool error"
        assert assisted["data"]["structuredContent"]["data"]["state"] == state, (
            "Legacy Assist lost the MCP result envelope or actual Core state"
        )
        checks.passed("legacy_Assist_uses_the_bundled_MCP_client_and_preserves_results")
        renewed = await _refresh(checks, client_id, envelope)
        assert renewed.status_code == 200, (
            "Native grant did not survive rollback to legacy"
        )
        checks.passed(
            "native_candidate_runs_real_legacy_tool_and_preserves_OAuth_grant_on_Core_2026_10_2"
        )

        await checks.control("legacy_rollback", {"action": "return"})
        returned = True
        native = await _ready(checks, "native")
        _preserved(native)
        for key in (
            "native_options_restored",
            "fixture_token_revoked",
            "fixture_credential_removed",
            "guard_enabled",
        ):
            assert native[key], f"Native return did not establish {key}"
        assert not native["legacy_thread"] and not native["legacy_listener"], (
            "Legacy listener or worker survived the native return"
        )
        restored = await checks.ready()
        assert checks.path == path and restored["manager_type"] == "NativeServerManager"
        native_read = await checks.call(
            "ha_get_state", {"entity_id": "input_boolean.native_light"}
        )
        assert native_read["data"]["state"] == state, "Native return lost Core state"
        renewed = await _refresh(checks, client_id, envelope)
        assert renewed.status_code == 200, (
            "The original grant did not survive the native return"
        )
        await _native_image(checks)
        checks.passed(
            "legacy_to_native_preserves_entry_artifacts_state_grants_and_rich_results_without_internal_token"
        )
    except BaseException as error:
        primary = error
        raise
    finally:
        await _cleanup_rollback(
            checks, envelope, restore=started and not returned, primary=primary
        )
