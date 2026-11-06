"""Native refusals keep actionable diagnostics without inventing write outcomes."""

import importlib
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.config_hash import compute_config_hash

from .test_component_ws_search import _REAL_VOL
from .test_native_blueprint_management import blueprints as blueprints
from .test_native_energy import call as energy_call
from .test_native_energy import energy as energy
from .test_native_engine_backend import native as native
from .test_native_full_backup_restore import restore as restore
from .test_native_full_backups import backups as backups
from .test_native_hacs import call as hacs_call
from .test_native_hacs import hacs as hacs
from .test_native_labels import _setup as label_setup
from .test_native_updates import invoke
from .test_native_updates import repairs as repairs
from .test_native_updates import updates as updates


@pytest.mark.parametrize("action", ["list", "get"])
async def test_blueprint_invalid_domain_explains_the_invalid_parameter(
    blueprints: Any, action: str
) -> None:
    service, owner, _, _ = blueprints
    with pytest.raises(ToolFailure, match="Invalid domain") as failed:
        await service.manage(action, domain="sensor", path="missing.yaml")
    assert failed.value.code == "VALIDATION_INVALID_PARAMETER"
    assert failed.value.data["valid_domains"] == ["automation", "script"]
    assert failed.value.status == "not_started" and not owner.calls


async def test_blueprint_missing_file_suggests_discovery(blueprints: Any) -> None:
    service, owner, _, _ = blueprints
    with pytest.raises(ToolFailure) as failed:
        await service.manage("get", path="missing.yaml")
    assert failed.value.code == "RESOURCE_NOT_FOUND"
    assert "ha_manage_blueprints" in failed.value.data["error"]["suggestion"]
    assert not owner.calls


async def test_blueprint_fetch_failure_suggests_verifying_url_without_saving(
    blueprints: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, owner, _, _ = blueprints
    fetch = AsyncMock(side_effect=FileNotFoundError("Blueprint not found"))
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.blueprint.importer",
        SimpleNamespace(fetch_blueprint_from_url=fetch),
    )
    with pytest.raises(ToolFailure) as failed:
        await service.manage("import", url="https://example.test/missing.yaml")
    assert failed.value.status == "not_started"
    assert "URL" in failed.value.data["error"]["suggestions"][0]
    assert fetch.await_count == 1 and not owner.calls


async def test_missing_label_explains_how_to_create_instead(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _, labels, _, _, _, _ = label_setup(native, monkeypatch)
    result = await engine.call(
        "ha_config_set_label",
        {"label_id": "absent", "name": "New"},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "not_started"
    assert "omit label_id" in result.data["error"]["suggestion"]
    assert labels.calls == []


@pytest.mark.parametrize("dry_run", [True, False])
async def test_energy_schema_rejection_identifies_core_and_has_no_effect(
    energy: Any, dry_run: bool
) -> None:
    def invalid(message: dict) -> dict:
        raise _REAL_VOL.Invalid("required key missing")

    energy.hass.data["websocket_api"]["energy/save_prefs"] = (object(), invalid)
    result = await energy_call(
        energy,
        {
            "mode": "set",
            "config": {"energy_sources": []},
            "config_hash": compute_config_hash(energy.owner.data),
            "dry_run": dry_run,
        },
    )
    assert result.is_error and result.status == "not_started"
    assert "Core rejected" in result.data["error"]["message"]
    assert not energy.owner.calls


@pytest.mark.parametrize("action", ["info", "download"])
async def test_hacs_absent_repository_reports_not_found(hacs: Any, action: str) -> None:
    result = await hacs_call(hacs, action, repository_id="absent/repository")
    assert result.is_error and result.status == "not_started"
    assert "not found" in result.data["error"]["message"].lower()


async def test_invalid_update_action_is_identified(updates: Any) -> None:
    result = await invoke(updates, action="uninstall", entity_ids="update.absent")
    assert result.is_error and result.status == "not_started"
    assert "Invalid action" in result.data["error"]["message"]
    assert not updates.calls


async def test_missing_update_has_a_discovery_hint(updates: Any) -> None:
    result = await invoke(updates, action="get", entity_ids="update.absent")
    assert result.is_error and result.status == "not_started"
    assert "ha_manage_updates" in result.data["error"]["suggestion"]


@pytest.mark.parametrize(
    ("action", "code"),
    [
        ("ignore_repair", "RESOURCE_NOT_FOUND"),
        ("unignore_repair", "SERVICE_CALL_FAILED"),
    ],
)
async def test_unknown_repair_keeps_the_public_per_item_code(
    repairs: Any, action: str, code: str
) -> None:
    result = await invoke(
        repairs.world,
        action=action,
        repairs=[
            {"domain": "fixture", "issue_id": "missing"},
            {"domain": "fixture", "issue_id": "a"},
        ],
    )
    assert result.data["results"][0]["error"]["code"] == code
    assert result.data["results"][1]["success"] is True
    assert repairs.calls == [("fixture", "a", action == "ignore_repair")]


async def test_missing_restore_archive_reports_not_found_before_safety_backup(
    restore: Any,
) -> None:
    with pytest.raises(ToolFailure, match="not found") as failed:
        await restore.restore(restore.manager, CallContext("actor"), "absent", False)
    assert "ha_manage_backup" in failed.value.data["error"]["suggestion"]
    assert "available backups" in failed.value.data["error"]["suggestion"]
    restore.owner.async_create_backup.assert_not_awaited()
    restore.owner.async_restore_backup.assert_not_awaited()


async def test_missing_screenshot_engine_has_configuration_guidance(
    native: Any,
) -> None:
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_screenshots"
    )
    manager = SimpleNamespace(
        policy=SimpleNamespace(flags={}),
        hass=SimpleNamespace(config=SimpleNamespace(components=set())),
    )
    service = module.NativeScreenshots(manager, CallContext("actor"))
    with pytest.raises(ToolFailure) as failed:
        await service._target()
    assert failed.value.status == "not_started"
    assert "engine" in failed.value.data["error"]["suggestions"][0]
