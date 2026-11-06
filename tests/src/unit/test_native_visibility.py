"""Native entity visibility guards every engine surface before projection."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools import native_visibility  # noqa: E402
from custom_components.ha_mcp_tools.native_approvals import NativeApprovalGate  # noqa: E402
from custom_components.ha_mcp_tools.native_settings import NativeSettings  # noqa: E402
from ha_mcp.policy.model import Policy, Rule  # noqa: E402
from ha_mcp.runtime import Engine  # noqa: E402
from ha_mcp.tool_api import CallContext, ToolDefinition, ToolResult  # noqa: E402
from ha_mcp.visibility.model import VisibilityConfig  # noqa: E402


def runtime(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    payload: dict,
    *,
    enforce: bool = True,
):
    (tmp_path / "entity_visibility.json").write_text(
        json.dumps(
            {"enabled": True, "enforce": enforce, "deny_entity_ids": ["sensor.secret"]}
        )
    )

    async def executor(function, *args):
        return function(*args)

    hass = SimpleNamespace(
        async_add_executor_job=executor,
        bus=SimpleNamespace(async_fire=MagicMock()),
        config=SimpleNamespace(path=lambda _subdir: str(tmp_path)),
    )
    manager = SimpleNamespace(
        hass=hass,
        settings_ui=SimpleNamespace(directory=tmp_path),
        refresh_policy=AsyncMock(),
        policy=SimpleNamespace(security_policy=None),
    )
    manager.visibility = native_visibility.NativeVisibility(manager)
    from homeassistant.helpers import entity_registry as er

    manager.registry = SimpleNamespace(entries={})
    manager.registry.async_get = manager.registry.entries.get
    monkeypatch.setattr(er, "async_get", lambda _hass: manager.registry)
    handler = AsyncMock(return_value=payload)
    manager.approval_gate = NativeApprovalGate(manager)
    engine = Engine(
        SimpleNamespace(authorize=AsyncMock()),
        [ToolDefinition(name, name, {}, handler)],
        policy_guard=manager.approval_gate,
        result_filter=manager.visibility.after,
    )
    monkeypatch.setattr(
        native_visibility, "hidden_from_core", lambda _hass, _config: {"sensor.secret"}
    )
    return manager, engine, handler


def hide_renamed_items(manager: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    identities = {
        "script.renamed_private": "PrivateKey",
        "scene.renamed_private": "1700000000001",
        "automation.renamed_private": "1700000000002",
    }
    manager.registry.entries.update(
        {
            entity_id: SimpleNamespace(
                entity_id=entity_id, unique_id=unique_id, aliases={"bogus_key"}
            )
            for entity_id, unique_id in identities.items()
        }
    )
    manager.registry.entries["script.visible"] = SimpleNamespace(
        entity_id="script.visible", unique_id="public_key", aliases={"PrivateKey"}
    )
    manager.visibility._config = AsyncMock(
        return_value=VisibilityConfig(
            enabled=True, enforce=True, deny_entity_ids=list(identities)
        )
    )
    monkeypatch.setattr(
        native_visibility, "hidden_from_core", lambda *_: set(identities)
    )
    return identities


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("ha_config_get_script", {"script_id": "PrivateKey"}),
        ("ha_config_get_scene", {"scene_id": "1700000000001"}),
        ("ha_config_get_automation", {"identifier": "1700000000002"}),
        ("ha_config_get_automation", {"entity_id": "1700000000002"}),
        (
            "ha_config_get_automation",
            {"identifier": "1700000000002", "entity_id": "visible"},
        ),
        ("ha_get_automation_traces", {"automation_id": "automation.1700000000002"}),
        ("ha_call_service", {"domain": "script", "service": "privatekey"}),
        (
            "ha_call_service",
            {
                "domain": "trace",
                "service": "get",
                "data": {"domain": "script", "item_id": "PrivateKey"},
            },
        ),
    ],
)
async def test_hidden_storage_identifiers_stop_before_global_approval_or_execution(
    tmp_path, monkeypatch: pytest.MonkeyPatch, name: str, arguments: dict
) -> None:
    manager, engine, handler = runtime(tmp_path, monkeypatch, name, {})
    hide_renamed_items(manager, monkeypatch)
    manager.policy.security_policy = Policy(rules=[Rule(tool_name="*")], wait_seconds=5)
    arguments["identifier_aliases"] = {"hidden": [], "script_services": []}
    result = await engine.call(name, arguments, CallContext("alice"))
    assert result.status == "not_started" and result.is_error, result
    assert result.data["error"]["code"] == "ENTITY_NOT_FOUND"
    handler.assert_not_awaited()
    assert not manager.approval_gate.queue.list_pending()
    manager.hass.bus.async_fire.assert_not_called()
    assert "renamed_private" not in json.dumps(result.data)


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "script.privatekey", "sensitive": "private-data"},
        {
            "trace": {
                "domain": "script",
                "item_id": "PrivateKey",
                "sensitive": "private-data",
            }
        },
        {
            "trace": {
                "domain": "automation",
                "item_id": 1700000000002,
                "sensitive": "private-data",
            }
        },
    ],
)
async def test_global_output_barrier_conceals_hidden_service_and_trace_aliases(
    tmp_path, monkeypatch: pytest.MonkeyPatch, payload: dict
) -> None:
    manager, engine, handler = runtime(
        tmp_path, monkeypatch, "ha_get_overview", payload
    )
    identities = hide_renamed_items(manager, monkeypatch)
    result = await engine.call("ha_get_overview", {}, CallContext("alice"))
    assert result.is_error and result.status == "confirmed", result
    assert result.data["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"
    assert "private-data" not in json.dumps(result.data)
    assert all(value not in json.dumps(result.data) for value in identities.values())
    handler.assert_awaited_once()


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("ha_config_get_script", {"script_id": "privatekey"}),
        ("ha_config_get_script", {"script_id": "public_key"}),
        ("ha_config_get_script", {"script_id": "bogus_key"}),
        ("ha_call_service", {"domain": "light", "service": "privatekey"}),
        ("ha_eval_template", {"template": "PrivateKey"}),
    ],
)
async def test_global_alias_barrier_trusts_core_keys_and_identifier_context_only(
    tmp_path, monkeypatch: pytest.MonkeyPatch, name: str, arguments: dict
) -> None:
    manager, engine, handler = runtime(
        tmp_path, monkeypatch, name, {"value": "PrivateKey"}
    )
    hide_renamed_items(manager, monkeypatch)
    arguments["identifier_aliases"] = {"script_services": ["public_key"]}
    result = await engine.call(name, arguments, CallContext("alice"))
    assert not result.is_error, result
    handler.assert_awaited_once()


async def test_global_matcher_detaches_core_aliases_before_executor_dispatch(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _engine, _handler = runtime(tmp_path, monkeypatch, "ha_get_state", {})
    identities = hide_renamed_items(manager, monkeypatch)
    hidden = set(identities)
    monkeypatch.setattr(native_visibility, "hidden_from_core", lambda *_: hidden)
    loop_thread = threading.get_ident()

    def read_registry(entity_id: str) -> Any:
        assert threading.get_ident() == loop_thread
        return manager.registry.entries.get(entity_id)

    manager.registry.async_get = read_registry

    async def executor(function, *args):
        hidden.clear()
        for entry in manager.registry.entries.values():
            entry.unique_id = "changed_after_capture"
        return await asyncio.to_thread(function, *args)

    manager.hass.async_add_executor_job = executor
    matcher = await manager.visibility._matcher(
        VisibilityConfig(enabled=True, enforce=True)
    )
    assert matcher.hidden == frozenset(identities)
    for entity_id, unique_id in identities.items():
        domain = entity_id.partition(".")[0]
        assert matcher.identifier_aliases.addresses_hidden(domain, unique_id)
        assert not matcher.identifier_aliases.addresses_hidden(
            domain, "changed_after_capture"
        )
    assert matcher.embeds("action: script.privatekey")
    assert not matcher.embeds("action: script.public_key")
    assert not matcher.embeds("action: script.changed_after_capture")


async def test_global_alias_registry_failure_conceals_output_and_stops_execution(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, engine, handler = runtime(
        tmp_path, monkeypatch, "ha_get_overview", {"sensitive": "private-data"}
    )
    identities = hide_renamed_items(manager, monkeypatch)

    def unavailable(entity_id: str) -> Any:
        raise RuntimeError("registry unavailable")

    manager.registry.async_get = unavailable
    before = await engine.call("ha_get_overview", {}, CallContext("alice"))
    after = await manager.visibility.after(
        "ha_get_overview",
        {},
        CallContext("alice"),
        ToolResult({"sensitive": "private-data"}),
    )
    for result in (before, after):
        assert result.is_error
        assert result.data["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"
        assert "private-data" not in json.dumps(result.data)
        assert all(
            value not in json.dumps(result.data) for value in identities.values()
        )
    assert before.status == "not_started"
    handler.assert_not_awaited()


async def test_hidden_argument_is_concealed_before_approval_and_execution(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, engine, handler = runtime(tmp_path, monkeypatch, "ha_call_service", {})
    manager.policy.security_policy = Policy(rules=[Rule(tool_name="*")])
    result = await engine.call(
        "ha_call_service", {"entity_id": "sensor.secret"}, CallContext("alice")
    )
    assert result.status == "not_started" and result.is_error
    assert result.data["error"]["code"] == "ENTITY_NOT_FOUND"
    handler.assert_not_awaited()
    assert manager.approval_gate.queue.list_pending() == []
    manager.hass.bus.async_fire.assert_not_called()


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("ha_manage_app", {"slug": "fixture", "path": "/read", "method": "GET"}),
        ("ha_manage_app", {"slug": "fixture", "action": "start"}),
        ("ha_manage_hacs", {"action": "download", "repository": "fixture/example"}),
        ("ha_manage_hacs", {"action": "remove", "repository": "fixture/example"}),
        ("ha_manage_ha_mcp_updates", {"action": "update"}),
        (
            "ha_manage_backup",
            {"scope": "snapshot", "action": "restore", "backup_id": "fixture"},
        ),
    ],
)
async def test_opaque_native_app_and_hacs_writes_stop_before_execution(
    tmp_path, monkeypatch, name, arguments
) -> None:
    manager, engine, handler = runtime(tmp_path, monkeypatch, name, {})
    result = await engine.call(name, arguments, CallContext("alice"))
    assert result.is_error and result.status == "not_started", result
    assert result.data["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"
    handler.assert_not_awaited()
    assert not manager.approval_gate.queue.list_pending()


@pytest.mark.parametrize(
    ("name", "action"),
    [
        ("ha_manage_hacs", "add_repository"),
        ("ha_manage_hacs", "update_information"),
        ("ha_manage_ha_mcp_updates", "check"),
        ("ha_manage_ha_mcp_updates", "guide"),
    ],
)
async def test_native_hacs_metadata_remains_available_under_visibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, action: str
) -> None:
    _manager, engine, handler = runtime(tmp_path, monkeypatch, name, {})
    result = await engine.call(name, {"action": action}, CallContext("alice"))
    assert not result.is_error
    handler.assert_awaited_once()


async def test_nested_self_update_cannot_inherit_a_parents_visibility_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manager, engine, handler = runtime(
        tmp_path, monkeypatch, "ha_manage_ha_mcp_updates", {}, enforce=False
    )

    async def parent(_arguments: dict, actor: CallContext) -> ToolResult:
        (tmp_path / "entity_visibility.json").write_text(
            json.dumps(
                {"enabled": True, "enforce": True, "deny_entity_ids": ["sensor.secret"]}
            )
        )
        return await engine.call(
            "ha_manage_ha_mcp_updates", {"action": "update"}, actor
        )

    engine.register(ToolDefinition("workflow", "Workflow", {}, parent))
    result = await engine.call("workflow", {}, CallContext("alice"))
    assert result.is_error and result.status == "not_started"
    assert result.data["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"
    handler.assert_not_awaited()


async def test_visible_state_scrubs_related_hidden_data_with_warning(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "success": True,
        "state": {
            "entity_id": "sensor.visible",
            "state": "ready",
            "attributes": {"related": "sensor.secret", "location": "private"},
        },
    }
    _manager, engine, _handler = runtime(tmp_path, monkeypatch, "ha_get_state", payload)
    result = await engine.call(
        "ha_get_state", {"entity_id": "sensor.visible"}, CallContext("alice")
    )
    assert not result.is_error
    assert result.data["state"]["state"] == "ready"
    assert "attributes" not in result.data["state"]
    assert result.data["warnings"]
    assert "sensor.secret" not in json.dumps(result.data)


async def test_outbound_refusal_preserves_completed_write_status(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _manager, engine, handler = runtime(
        tmp_path,
        monkeypatch,
        "native_write",
        {"related": "sensor.secret", "sensitive": "do not disclose"},
    )
    result = await engine.call("native_write", {}, CallContext("alice"))
    assert result.is_error and result.status == "confirmed"
    assert result.data["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"
    assert "do not disclose" not in json.dumps(result.data)
    handler.assert_awaited_once()


async def test_declutter_mode_keeps_direct_read_contract(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _manager, engine, _handler = runtime(
        tmp_path,
        monkeypatch,
        "ha_get_state",
        {"entity_id": "sensor.secret"},
        enforce=False,
    )
    result = await engine.call(
        "ha_get_state", {"entity_id": "sensor.secret"}, CallContext("alice")
    )
    assert result.data["entity_id"] == "sensor.secret" and not result.is_error


@pytest.mark.parametrize(
    "tool,arguments,enforce,denied",
    [
        (
            "ha_manage_pipeline",
            {"action": "process", "sentence": "What is happening in the house?"},
            True,
            True,
        ),
        ("ha_manage_pipeline", {"action": "list"}, True, False),
        (
            "ha_manage_pipeline",
            {"action": "process", "sentence": "What is happening in the house?"},
            False,
            False,
        ),
        (
            "ha_manage_radio",
            {"radio": "matter", "action": "diagnostics", "device_id": "opaque-node"},
            True,
            True,
        ),
        (
            "ha_manage_radio",
            {"radio": "matter", "action": "diagnostics", "device_id": "opaque-node"},
            False,
            False,
        ),
    ],
)
async def test_opaque_operations_cannot_bypass_hidden_entities_without_naming_them(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    arguments: dict,
    enforce: bool,
    denied: bool,
) -> None:
    _manager, engine, handler = runtime(
        tmp_path,
        monkeypatch,
        tool,
        {"speech": "Someone is at home"},
        enforce=enforce,
    )
    result = await engine.call(
        tool,
        arguments,
        CallContext("alice"),
    )
    if denied:
        assert result.is_error and result.status == "not_started"
        assert result.data["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"
        handler.assert_not_awaited()
    else:
        assert not result.is_error
        handler.assert_awaited_once()


@pytest.mark.parametrize(
    "listeners,enforce,denied",
    [
        ({"all": True, "domains": [], "entities": []}, True, True),
        ({"all": False, "domains": ["sensor"], "entities": []}, True, True),
        ({"all": False, "domains": ["light"], "entities": []}, True, False),
        ({"all": True, "domains": [], "entities": []}, False, False),
    ],
)
async def test_template_scalar_uses_core_dependency_scope_before_disclosure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    listeners: dict,
    enforce: bool,
    denied: bool,
) -> None:
    payload = {
        "success": True,
        "result": "sensitive-derived-value",
        "listeners": listeners,
    }
    _manager, engine, handler = runtime(
        tmp_path, monkeypatch, "ha_eval_template", payload, enforce=enforce
    )
    result = await engine.call(
        "ha_eval_template",
        {
            "template": "{{ states[domain] | map(attribute='state') | list }}",
            "variables": {"domain": "sensor"},
        },
        CallContext("alice"),
    )
    assert result.is_error is denied
    assert result.status == "confirmed"
    handler.assert_awaited_once()
    if denied:
        assert result.data["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"
        assert "sensor.secret" not in json.dumps(result.data)
        assert "sensitive-derived-value" not in json.dumps(result.data)
    else:
        assert result.data == payload


async def test_corrupt_visibility_keeps_last_enforced_boundary(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _manager, engine, handler = runtime(tmp_path, monkeypatch, "ha_get_state", {})
    first = await engine.call(
        "ha_get_state", {"entity_id": "sensor.secret"}, CallContext("alice")
    )
    (tmp_path / "entity_visibility.json").write_text("{")
    second = await engine.call(
        "ha_get_state", {"entity_id": "sensor.secret"}, CallContext("alice")
    )
    assert first.is_error and second.is_error and second.status == "not_started"
    handler.assert_not_awaited()


def test_core_hidden_set_reuses_registry_categories_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_mcp_tools import core_registry as registry

    hidden = SimpleNamespace(
        entity_id="sensor.secret",
        entity_category="diagnostic",
        hidden_by=None,
        labels=set(),
        area_id=None,
        device_id=None,
    )
    visible = SimpleNamespace(
        entity_id="sensor.visible",
        entity_category=None,
        hidden_by=None,
        labels=set(),
        area_id=None,
        device_id=None,
    )
    view = registry._RegistryView(
        entity=SimpleNamespace(
            entities={hidden.entity_id: hidden, visible.entity_id: visible}
        )
    )
    monkeypatch.setattr(registry, "_resolve_registries", lambda _hass: view)
    hass = SimpleNamespace(states=SimpleNamespace(async_all=lambda: [hidden, visible]))
    assert native_visibility.hidden_from_core(
        hass, VisibilityConfig(enabled=True, enforce=True)
    ) == {"sensor.secret"}


def test_visibility_ui_save_checks_version_and_preserves_dimensions(tmp_path) -> None:
    ui = NativeSettings(
        SimpleNamespace(
            hass=SimpleNamespace(
                config=SimpleNamespace(path=lambda _subdir: str(tmp_path))
            )
        )
    )
    result = ui._save_visibility(
        {
            "version": 1,
            "enabled": True,
            "enforce": True,
            "allow_entity_ids": ["sensor.allowed"],
        }
    )
    assert result["version"] == 2
    stale = ui._save_visibility({"version": 1, "enabled": False})
    assert stale["current_version"] == 2
    config = native_visibility.load_native_visibility(tmp_path)
    assert (
        config.enabled
        and config.enforce
        and config.allow_entity_ids == ["sensor.allowed"]
    )
