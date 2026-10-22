"""Qualify Assist storage and text conversation against the actual Core owners.

Requires pipeline_support's setup/probe/cleanup fixture hooks. The fixture agent
executes a real Core service with the actor's Context; no websocket handler or
loopback client participates. The tool remains text-only, not a STT/TTS run.
"""

import json
from typing import Any


async def _failed(checks: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    body = await checks.rpc(
        "tools/call", {"name": "ha_manage_pipeline", "arguments": arguments}
    )
    assert "error" not in body and body["result"].get("isError"), body
    result = body["result"]
    return result.get("structuredContent") or json.loads(
        next(block["text"] for block in result["content"] if block["type"] == "text")
    )


async def exercise_pipelines(checks: Any) -> None:
    await checks.control("pipeline_seed", {})
    initial = await checks.call("ha_manage_pipeline", {"action": "list"})
    probe = await checks.control("pipeline_probe")
    assert initial["pipelines"] == probe["pipelines"], (initial, probe)
    preferred = initial["preferred_pipeline"]
    original = await checks.call(
        "ha_manage_pipeline", {"action": "get", "pipeline_id": preferred}
    )
    assert original["pipeline"]["id"] == preferred, original
    clone = await checks.call(
        "ha_manage_pipeline",
        {
            "action": "create",
            "name": "Native Assist fixture",
            "conversation_engine": probe["agent_id"],
            "conversation_language": "*",
            "language": "en",
            "make_preferred": True,
        },
    )
    identifier = clone["pipeline_id"]
    try:
        assert clone["pipeline"]["id"] != preferred and clone["preferred_changed"], (
            clone
        )
        readback = await checks.call(
            "ha_manage_pipeline", {"action": "get", "pipeline_id": identifier}
        )
        assert readback["pipeline"] == clone["pipeline"], (clone, readback)
        assert (await checks.control("pipeline_probe"))[
            "preferred_pipeline"
        ] == identifier
        updated = await checks.call(
            "ha_manage_pipeline",
            {
                "action": "update",
                "pipeline_id": identifier,
                "name": "Native Assist updated",
                "tts_voice": "",
            },
        )
        assert (
            updated["pipeline"]["tts_voice"] is None
            and updated["pipeline"]["name"] == "Native Assist updated"
        ), updated
        checks.passed(
            "native_pipeline_Core_collection_create_update_preferred_and_readback"
        )

        bad = await _failed(
            checks,
            {
                "action": "update",
                "pipeline_id": identifier,
                "stt_engine": "invalid_fixture_engine",
                "stt_language": "",
            },
        )
        assert bad["error"], bad
        unchanged = await checks.call(
            "ha_manage_pipeline", {"action": "get", "pipeline_id": identifier}
        )
        assert unchanged["pipeline"] == updated["pipeline"], (bad, unchanged)
        checks.passed(
            "native_pipeline_real_Core_language_validation_rejects_before_mutation"
        )

        await checks.call(
            "ha_call_service",
            {
                "domain": "input_boolean",
                "service": "turn_off",
                "entity_id": "input_boolean.native_light",
            },
        )
        processed = await checks.call(
            "ha_manage_pipeline",
            {
                "action": "process",
                "sentence": "Run the native fixture",
                "pipeline_id": identifier,
            },
        )
        assert (
            processed["success"]
            and processed["speech"] == "Native Assist command completed"
        ), processed
        first = await checks.control("pipeline_probe")
        assert first["state"] == "on" and first["calls"][-1]["language"] == "en", first
        assert first["calls"][-1]["user_id"] == first["state_user_id"], first
        assert first["calls"][-1]["user_id"], first
        again = await checks.call(
            "ha_manage_pipeline",
            {
                "action": "process",
                "sentence": "Continue the native fixture",
                "agent_id": probe["agent_id"],
                "language": "en",
                "conversation_id": processed["conversation_id"],
            },
        )
        last = await checks.control("pipeline_probe")
        assert again["conversation_id"] == processed["conversation_id"]
        assert last["calls"][-1]["conversation_id"] == processed["conversation_id"]
        checks.passed(
            "native_pipeline_Core_conversation_service_effect_actor_and_continuation"
        )
        await checks.call(
            "ha_manage_pipeline", {"action": "set_preferred", "pipeline_id": preferred}
        )
        assert (await checks.control("pipeline_probe"))[
            "preferred_pipeline"
        ] == preferred
    finally:
        await checks.control(
            "pipeline_cleanup",
            {"pipeline_id": identifier, "preferred_pipeline": preferred},
        )
