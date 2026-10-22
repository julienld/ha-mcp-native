"""Native entity visibility guards every engine surface before projection."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools import native_visibility  # noqa: E402
from custom_components.ha_mcp_tools.native_approvals import NativeApprovalGate  # noqa: E402
from custom_components.ha_mcp_tools.native_settings import NativeSettings  # noqa: E402
from ha_mcp.policy.model import Policy, Rule  # noqa: E402
from ha_mcp.runtime import Engine  # noqa: E402
from ha_mcp.tool_api import CallContext, ToolDefinition  # noqa: E402
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
    from custom_components.ha_mcp_tools.websocket_api import registry

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
