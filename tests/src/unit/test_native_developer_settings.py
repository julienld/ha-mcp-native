"""Native developer calls patch real settings while preserving their authority."""

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext, ToolDefinition

from .test_native_engine_backend import native as native
from .test_native_file_access import files as files
from .test_native_file_settings import panel as panel
from .test_native_filesystem import filesystem as filesystem


@pytest.fixture
def developer(panel):
    from custom_components.ha_mcp_tools.native_dev_tools import dev_definitions
    from custom_components.ha_mcp_tools.native_operations.developer_server import (
        NativeDeveloperServer,
    )
    from custom_components.ha_mcp_tools.native_server_policy import load_policy

    settings, _owner = panel
    manager = settings.manager
    manager.settings_ui = settings
    settings._save_features({"flags": {"enable_strict_mandatory_bps": False}})
    settings._save_advanced({"enable_dev_mode": True})
    manager.approval_gate = SimpleNamespace(
        queue=SimpleNamespace(clear_remember_cache=MagicMock())
    )
    manager.system_lifecycle = SimpleNamespace(schedule=MagicMock())
    manager.entry = SimpleNamespace(entry_id="native")

    async def refresh():
        manager.policy = await asyncio.to_thread(load_policy, settings.directory)

    manager.refresh_policy = AsyncMock(side_effect=refresh)
    manager.policy = load_policy(settings.directory)
    definitions = dev_definitions(manager)
    definitions.append(
        ToolDefinition(
            "ha_call_service", "service", {"type": "object"}, AsyncMock(), {}
        )
    )
    manager.engine = Engine(manager.backend, definitions)
    return NativeDeveloperServer(manager, CallContext("owner")), manager


def test_tool_settings_never_reacquire_the_configuration_lock(developer, monkeypatch):
    owner, manager = developer
    import ha_mcp.utils.config_write_lock as locks

    manager.settings_ui._save_features(
        {"flags": {"enable_tool_security_policies": True}}
    )
    held = False

    @contextmanager
    def exclusive(_directory):
        nonlocal held
        assert not held, "nested configuration lock"
        held = True
        try:
            yield
        finally:
            held = False

    monkeypatch.setattr(locks, "config_file_lock", exclusive)
    manager.settings_ui._save_tools({"states": {"ha_call_service": "pinned"}})
    assert (
        json.loads((owner.ui.directory / "tool_config.json").read_text())["tools"][
            "ha_call_service"
        ]
        == "pinned"
    )


async def call(manager, **arguments):
    return await manager.engine.call(
        "ha_dev_manage_settings", arguments, CallContext("owner")
    )


async def test_native_tool_patch_preserves_other_states_exposure_and_unknown_fields(
    developer,
):
    owner, manager = developer
    path = owner.ui.directory / "tool_config.json"
    path.write_text(
        json.dumps(
            {
                "tools": {"ha_get_state": "disabled"},
                "llm_api": {"ha_get_state": False},
                "future": {"keep": True},
            }
        )
    )
    result = await call(
        manager,
        action="set_tool",
        tool="ha_call_service",
        state="pinned",
        llm_api=False,
    )
    assert not result.is_error, result.data
    saved = json.loads(path.read_text())
    assert saved == {
        "tools": {"ha_get_state": "disabled", "ha_call_service": "pinned"},
        "llm_api": {"ha_get_state": False, "ha_call_service": False},
        "future": {"keep": True},
    }


def test_admin_bindings_preserve_frozen_public_contracts(developer):
    _owner, manager = developer
    from custom_components.ha_mcp_tools.native_report_tools import report_definition
    from custom_components.ha_mcp_tools.native_security_policy_tools import (
        security_policy_definition,
    )

    from .test_native_catalog_contract import (
        _ANNOTATIONS,
        _changed_inputs,
        _structural_schema,
    )

    baseline = {
        row["name"]: row
        for row in json.loads(
            (
                Path(__file__).resolve().parents[3]
                / "docs/native-migration/catalog-baseline.json"
            ).read_text()
        )["tools"]
    }
    for definition in [
        *(
            tool
            for name, tool in manager.engine.tools.items()
            if name.startswith("ha_dev_")
        ),
        report_definition(manager),
        security_policy_definition(manager),
    ]:
        old = baseline[definition.name]
        assert not _changed_inputs(old["inputSchema"], definition.input_schema), (
            definition.name
        )
        assert _structural_schema(old.get("outputSchema")) == _structural_schema(
            definition.output_schema
        ), definition.name
        assert all(
            old["annotations"].get(hint) == definition.annotations.get(hint)
            for hint in _ANNOTATIONS
        ), definition.name


def test_backup_ui_exposes_deletion_controls_with_canonical_validation(developer):
    owner, _manager = developer
    saved = owner.ui._save_backup_config(
        {"enable_snapshot_delete": "true", "snapshot_delete_min_age_days": "14"}
    )
    assert saved["applied"] == {
        "enable_snapshot_delete": True,
        "snapshot_delete_min_age_days": 14,
    }
    rows = {row["field"]: row for row in owner.ui._backup_config_fields()}
    assert rows["enable_snapshot_delete"]["value"] is True
    assert rows["snapshot_delete_min_age_days"]["value"] == 14
    with pytest.raises(ValueError):
        owner.ui._save_backup_config({"snapshot_delete_min_age_days": -1})


async def test_dev_gate_denial_precedes_every_requested_write(developer):
    owner, manager = developer
    result = await call(
        manager,
        action="set_tool",
        tool="ha_call_service",
        state="disabled",
        gated=False,
    )
    assert (
        result.is_error
        and result.data["error"]["code"] == "AUTH_INSUFFICIENT_PERMISSIONS"
    )
    assert not (owner.ui.directory / "tool_config.json").exists()
    assert not (owner.ui.directory / "tool_policy.json").exists()


async def test_native_allow_list_wildcard_refuses_ineffective_gate_without_partial_state(
    developer,
):
    owner, manager = developer
    owner.ui._save_advanced({"dev_tools_security_policy_access": True})
    owner.ui._save_policy({"rules": [{"tool_name": "*"}], "rule_effect": "allow"})
    await manager.refresh_policy()
    result = await call(
        manager, action="set_tool", tool="ha_call_service", state="disabled", gated=True
    )
    assert result.is_error and "unconditional" in result.data["error"]["message"]
    assert not (owner.ui.directory / "tool_config.json").exists()


async def test_native_partial_tool_policy_failure_reports_confirmed_file_without_retry(
    developer, monkeypatch
):
    owner, manager = developer
    import custom_components.ha_mcp_tools.native_operations.developer_store as module

    owner.ui._save_advanced({"dev_tools_security_policy_access": True})
    await manager.refresh_policy()
    save = MagicMock(side_effect=OSError("disk refused"))
    monkeypatch.setattr(module, "save_policy_locked", save)
    result = await call(
        manager,
        action="set_tool",
        tool="ha_call_service",
        state="disabled",
        llm_api=False,
        gated=True,
    )
    assert result.is_error and result.status == "outcome_unknown"
    assert result.data["confirmed_files"] == ["tool_config.json"]
    assert (
        result.data["partial_commit"] and result.data["persisted"]["llm_api"] is False
    )
    assert (
        json.loads((owner.ui.directory / "tool_config.json").read_text())["tools"][
            "ha_call_service"
        ]
        == "disabled"
    )
    save.assert_called_once()


@pytest.mark.parametrize(
    "setting",
    [
        "enable_security_policy_tool",
        "dev_tools_security_policy_access",
        "custom_instructions",
    ],
)
@pytest.mark.parametrize("action", ["set", "reset"])
async def test_native_developer_cannot_change_human_only_controls_even_with_permission(
    developer, setting, action
):
    owner, manager = developer
    owner.ui._save_advanced({"dev_tools_security_policy_access": True})
    await manager.refresh_policy()
    before = (owner.ui.directory / "feature_flags.json").read_bytes()
    result = await call(
        manager,
        action=action,
        setting=setting,
        value="text" if setting == "custom_instructions" else False,
    )
    assert (
        result.is_error
        and result.data["error"]["code"] == "AUTH_INSUFFICIENT_PERMISSIONS"
    )
    assert (owner.ui.directory / "feature_flags.json").read_bytes() == before


async def test_native_backup_human_control_rejects_entire_patch(developer):
    owner, manager = developer
    result = await call(
        manager,
        action="set_backup_config",
        backup={"enable_auto_backup": False, "backup_read_only": False},
    )
    assert result.is_error and result.status == "not_started"
    assert not (owner.ui.directory / "backup_settings.json").exists()


@pytest.mark.parametrize(
    "field,value",
    [("enable_snapshot_delete", True), ("snapshot_delete_min_age_days", 0)],
)
async def test_native_delete_controls_cannot_be_changed_through_either_dev_surface(
    developer, field, value
):
    owner, manager = developer
    for arguments in (
        {
            "action": "set_backup_config",
            "backup": {field: value, "enable_auto_backup": False},
        },
        {"action": "set", "setting": field, "value": value},
        {"action": "reset", "setting": field},
    ):
        result = await call(manager, **arguments)
        assert result.is_error and result.status == "not_started", result
    assert not (owner.ui.directory / "backup_settings.json").exists()
    listed = await call(manager, action="get_backup_config")
    row = next(item for item in listed.data["data"]["fields"] if item["field"] == field)
    assert row["editable"] is False


async def test_native_settings_env_reset_and_beta_refused_before_persist(
    developer, monkeypatch
):
    owner, manager = developer
    monkeypatch.setenv("REDACT_SECRETS", "true")
    result = await call(manager, action="reset", setting="redact_secrets")
    assert result.is_error and "Environment" in result.data["error"]["message"]
    result = await call(manager, action="set", setting="enable_code_mode", value=True)
    assert result.is_error and "beta" in result.data["error"]["message"]
    assert "enable_code_mode" not in json.loads(
        (owner.ui.directory / "feature_flags.json").read_text()
    )


async def test_dev_self_disable_reports_commit_then_closes_next_call(developer):
    owner, manager = developer
    result = await call(manager, action="set", setting="enable_dev_mode", value=False)
    assert not result.is_error and result.data["data"]["value"] is False
    refused = await call(manager, action="list")
    assert refused.is_error and refused.data["error"]["code"] == "FEATURE_DISABLED"


async def test_dev_owned_worker_revalidates_before_executor_dispatch(
    developer, monkeypatch
):
    owner, manager = developer
    import custom_components.ha_mcp_tools.native_operations.developer_settings as module

    owned = module.await_owned_io
    original = owner.ui._save_features
    saved = MagicMock(wraps=original)
    monkeypatch.setattr(owner.ui, "_save_features", saved)

    async def revoke(awaitable):
        manager.backend.authorize.side_effect = PermissionError("revoked")
        return await owned(awaitable)

    monkeypatch.setattr(module, "await_owned_io", revoke)
    result = await call(manager, action="set", setting="redact_secrets", value=True)
    assert result.is_error and result.status == "not_started"
    saved.assert_not_called()


async def test_dev_revoked_after_commit_preserves_unknown_and_withholds_reply(
    developer, monkeypatch
):
    owner, manager = developer
    save = owner.ui._save_features

    def revoke(body, **kwargs):
        result = save(body, **kwargs)
        manager.backend.authorize.side_effect = PermissionError("revoked")
        return result

    monkeypatch.setattr(owner.ui, "_save_features", revoke)
    result = await call(manager, action="set", setting="redact_secrets", value=True)
    assert (
        result.is_error
        and result.status == "outcome_unknown"
        and result.data["write_committed"]
    )
    assert json.loads((owner.ui.directory / "feature_flags.json").read_text())[
        "redact_secrets"
    ]


async def test_developer_read_withholds_result_when_feature_is_disabled_during_io(
    developer, monkeypatch
):
    owner, manager = developer
    from custom_components.ha_mcp_tools.native_operations.developer_settings import (
        NativeDeveloperSettings,
    )

    rows = NativeDeveloperSettings._rows

    def disable(self):
        result = rows(self)
        self.ui._save_advanced({"enable_dev_mode": False})
        return result

    monkeypatch.setattr(NativeDeveloperSettings, "_rows", disable)
    result = await call(manager, action="list")
    assert result.is_error and result.data["error"]["code"] == "FEATURE_DISABLED"
    assert "settings" not in result.data


async def test_developer_reset_preserves_override_when_env_pin_appears_waiting_for_lock(
    developer, monkeypatch
):
    owner, manager = developer
    import custom_components.ha_mcp_tools.native_operations.developer_settings as settings_module

    owner.ui._save_features({"flags": {"redact_secrets": False}})
    original = settings_module.config_file_lock

    @contextmanager
    def pin_while_waiting(directory):
        with original(directory):
            monkeypatch.setenv("REDACT_SECRETS", "true")
            yield

    monkeypatch.setattr(settings_module, "config_file_lock", pin_while_waiting)
    result = await call(manager, action="reset", setting="redact_secrets")
    assert result.is_error and result.status == "not_started"
    assert (
        json.loads((owner.ui.directory / "feature_flags.json").read_text())[
            "redact_secrets"
        ]
        is False
    )


def test_native_configuration_reads_do_not_migrate_policy_files(developer):
    owner, _manager = developer
    from custom_components.ha_mcp_tools.native_server_policy import load_policy

    owner.ui._save_features({"flags": {"enable_tool_security_policies": True}})
    target = owner.ui.directory / "tool_policy.json"
    original = '{"rules": []}'
    target.write_text(original)
    load_policy(owner.ui.directory)
    assert target.read_text() == original


def test_native_initial_configuration_performs_existing_policy_migration_once(
    developer,
):
    owner, _manager = developer
    from custom_components.ha_mcp_tools.native_server_policy import (
        load_initial_policy,
        load_policy,
    )

    owner.ui._save_features({"flags": {"enable_tool_security_policies": True}})
    target = owner.ui.directory / "tool_policy.json"
    target.write_text('{"rules": []}')
    load_initial_policy(owner.ui.directory)
    migrated = target.read_bytes()
    assert json.loads(migrated)["schema_version"] >= 1
    load_policy(owner.ui.directory)
    assert target.read_bytes() == migrated


async def test_native_update_source_never_attempts_an_installer(developer):
    _owner, manager = developer
    result = await manager.engine.call(
        "ha_dev_manage_server",
        {"action": "update_source", "pip_spec": "ha-mcp==1.0"},
        CallContext("owner"),
    )
    assert (
        result.is_error
        and result.data["error"]["code"] == "UNSUPPORTED_OPERATION"
        and result.status == "not_started"
    )
    manager.system_lifecycle.schedule.assert_not_called()


async def test_native_restart_hands_actual_actor_and_entry_to_owned_lifecycle(
    developer,
):
    _owner, manager = developer
    actor = CallContext("owner", "assist", tool_guard=lambda _name: None)
    result = await manager.engine.call(
        "ha_dev_manage_server", {"action": "restart"}, actor
    )
    assert not result.is_error and result.data["data"]["restart_scheduled"]
    assert (
        result.data["data"]["scheduled"] and result.data["data"]["mode"] == "embedded"
    )
    manager.system_lifecycle.schedule.assert_called_once_with(
        "entry_reload", actor, entry=manager.entry
    )


async def test_native_dev_info_keeps_existing_version_and_entry_response_fields(
    developer, monkeypatch
):
    import sys

    owner, manager = developer
    monkeypatch.setitem(
        sys.modules, "homeassistant.const", SimpleNamespace(__version__="2026.10.2")
    )
    result = await manager.engine.call(
        "ha_dev_manage_server", {"action": "info"}, CallContext("owner")
    )
    assert not result.is_error, result
    data = result.data["data"]
    assert isinstance(data["server_version"], str) and isinstance(
        data["is_dev_build"], bool
    )
    assert (
        data["data_dir"] == str(owner.ui.directory)
        and data["ha_version"] == "2026.10.2"
    )
    assert (
        data["component_server_entry"]["entry_id"] == "native"
        and data["component_server_entry"]["pip_spec"] is None
    )


async def test_dev_rechecks_local_permission_after_waiting_for_file_lock(
    developer, monkeypatch
):
    owner, manager = developer
    import ha_mcp.utils.config_write_lock as locks

    original = locks.config_file_lock

    @contextmanager
    def disable_while_waiting(directory):
        with original(directory):
            path = directory / "feature_flags.json"
            values = json.loads(path.read_text())
            values["enable_dev_mode"] = False
            path.write_text(json.dumps(values))
            yield

    monkeypatch.setattr(locks, "config_file_lock", disable_while_waiting)
    result = await call(manager, action="set", setting="redact_secrets", value=True)
    assert (
        result.is_error
        and result.status == "not_started"
        and result.data["error"]["code"] == "FEATURE_DISABLED"
    )
    assert "redact_secrets" not in json.loads(
        (owner.ui.directory / "feature_flags.json").read_text()
    )


async def test_dev_approval_claims_existing_queue_once_and_requires_separate_permission(
    developer,
):
    owner, manager = developer
    from ha_mcp.policy.approval_queue import ApprovalQueue

    queue = ApprovalQueue()
    manager.approval_gate.queue = queue
    item = queue.create(
        "ha_call_service", "fixture-args-hash", {"entity_id": "light.a"}, ttl_minutes=10
    )
    result = await manager.engine.call(
        "ha_dev_manage_server",
        {"action": "approve", "token": item.token},
        CallContext("owner"),
    )
    assert result.is_error and item.decision == "pending"
    owner.ui._save_advanced({"dev_tools_security_policy_access": True})
    await manager.refresh_policy()
    result = await manager.engine.call(
        "ha_dev_manage_server",
        {"action": "approve", "token": item.token},
        CallContext("owner"),
    )
    assert not result.is_error and item.decision == "approved"
    repeated = await manager.engine.call(
        "ha_dev_manage_server",
        {"action": "deny", "token": item.token},
        CallContext("owner"),
    )
    assert repeated.is_error and item.decision == "approved"
