"""Qualify Energy schema, normalization, hashes and recorder metadata on Core."""

import json
from typing import Any


async def _refused(checks: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    body = await checks.rpc(
        "tools/call", {"name": "ha_manage_energy_prefs", "arguments": arguments}
    )
    assert "error" not in body and body["result"].get("isError"), body
    result = body["result"]
    return result.get("structuredContent") or json.loads(
        next(block["text"] for block in result["content"] if block["type"] == "text")
    )


async def exercise_energy(checks: Any) -> None:
    initial = await checks.call(
        "ha_manage_energy_prefs", {"mode": "get", "include_schema": True}
    )
    assert initial["core_contract"]["status"] == "available", initial
    preview = await checks.call(
        "ha_manage_energy_prefs",
        {
            "mode": "add_device",
            "stat_consumption": "native_fixture:energy",
            "dry_run": True,
        },
    )
    assert preview["proposal_validation"]["valid"] and preview["dry_run"], preview
    probe = await checks.control("energy_probe")
    assert probe["data"] is None, probe
    invalid = await _refused(
        checks,
        {"mode": "set", "config": {"device_consumption": [{}]}, "dry_run": True},
    )
    assert invalid["error"]["code"] == "VALIDATION_FAILED", invalid
    assert (await checks.control("energy_probe"))["data"] is None
    checks.passed(
        "native_energy_Core_schema_preview_never_saves_or_initializes_preferences"
    )

    added = await checks.call(
        "ha_manage_energy_prefs",
        {
            "mode": "add_device",
            "stat_consumption": "native_fixture:energy",
            "name": "Native device",
        },
    )
    assert added["success"] and added["new_count"] == 1, added
    enriched = await checks.call(
        "ha_manage_energy_prefs", {"mode": "get", "include_statistics": True}
    )
    assert enriched["config_hash"] == added["config_hash"], enriched
    metadata = enriched["statistics_metadata"][0]
    assert metadata["statistic_id"] == "native_fixture:energy", metadata
    assert (
        metadata["unit_source"] == "recorder_metadata"
        and metadata["unit_of_measurement"] == "kWh"
    ), metadata
    assert (await checks.control("energy_probe"))["data"] == enriched["config"]
    checks.passed(
        "native_energy_direct_Core_manager_save_and_real_SQLite_statistics_metadata"
    )

    source = {
        "type": "battery",
        "stat_energy_from": "native_fixture:energy",
        "stat_energy_to": "native_fixture:charge",
        "stat_rate": "sensor.native_overridden",
        "power_config": {"stat_rate": "sensor.native_power"},
    }
    normalized = await checks.call(
        "ha_manage_energy_prefs", {"mode": "add_source", "source": source}
    )
    saved = normalized["config"]
    assert saved["energy_sources"][0]["stat_rate"] == "sensor.native_power", normalized
    assert saved["energy_sources"][0]["power_config"] == source["power_config"], (
        normalized
    )
    assert saved["device_consumption"] == enriched["config"]["device_consumption"], (
        normalized
    )
    assert normalized.get("post_save_validation_errors"), normalized
    assert (await checks.control("energy_probe"))["data"] == saved
    checks.passed(
        "native_energy_Core_power_normalization_and_honest_post_save_semantic_issues"
    )

    stale = await _refused(
        checks,
        {
            "mode": "set",
            "config": {"energy_sources": []},
            "config_hash": initial["config_hash"],
        },
    )
    assert stale["error"]["code"] == "RESOURCE_LOCKED", stale
    assert (await checks.control("energy_probe"))["data"] == saved
    cleaned = await checks.call(
        "ha_manage_energy_prefs",
        {
            "mode": "set",
            "config": {"energy_sources": []},
            "config_hash": {
                "energy_sources": normalized["config_hash_per_key"]["energy_sources"]
            },
        },
    )
    assert cleaned["config"]["energy_sources"] == [], cleaned
    assert cleaned["config"]["device_consumption"] == saved["device_consumption"], (
        cleaned
    )
    await checks.call(
        "ha_manage_energy_prefs",
        {"mode": "remove_device", "stat_consumption": "native_fixture:energy"},
    )
    final = await checks.call("ha_manage_energy_prefs", {"mode": "get"})
    assert final["config"] == initial["config"], final
    checks.passed(
        "native_energy_full_and_per_key_hashes_preserve_siblings_and_refuse_stale_writes"
    )
