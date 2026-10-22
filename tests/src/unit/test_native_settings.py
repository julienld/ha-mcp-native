"""Native settings persist in HA's directory and preserve the same live gates."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools.native_server_policy import load_policy  # noqa: E402
from custom_components.ha_mcp_tools.native_settings import NativeSettings  # noqa: E402
from ha_mcp.policy.persistence import load_policy as load_approval_policy  # noqa: E402


def settings(tmp_path: Path) -> NativeSettings:
    hass = SimpleNamespace(config=SimpleNamespace(path=lambda _subdir: str(tmp_path)))
    return NativeSettings(SimpleNamespace(hass=hass))


def test_tool_toggles_apply_to_native_policy_and_stay_in_instance_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(elsewhere))
    ui = settings(tmp_path / "ha")
    ui._save_tools(
        {
            "states": {"ha_call_service": "disabled", "ha_list_services": "pinned"},
            "llm_api": {"ha_list_services": False},
        }
    )
    policy = load_policy(ui.directory)
    assert not policy.enabled("ha_call_service")
    assert "ha_list_services" in policy.pinned
    assert not policy.llm_overrides["ha_list_services"]
    assert not (elsewhere / "tool_config.json").exists()


def test_environment_pin_cannot_be_overwritten_and_is_not_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISABLED_TOOLS", "ha_list_services")
    ui = settings(tmp_path)
    with pytest.raises(ValueError, match="Environment-pinned"):
        ui._save_tools({"states": {"ha_list_services": "enabled"}})
    assert not (tmp_path / "tool_config.json").exists()
    ui._save_tools({"states": {"ha_list_services": "disabled"}})
    raw = json.loads((tmp_path / "tool_config.json").read_text())
    assert "ha_list_services" not in raw["tools"]
    assert not load_policy(tmp_path).enabled("ha_list_services")


def test_policy_compare_and_swap_preserves_newer_policy(tmp_path: Path) -> None:
    ui = settings(tmp_path)
    result, _changed = ui._save_policy(
        {"version": 0, "rules": [{"tool_name": "ha_call_service"}]}
    )
    assert result["version"] == 1
    stale, changed = ui._save_policy({"version": 0, "rules": []})
    assert stale["current_version"] == 1 and not changed
    assert load_approval_policy(tmp_path).rules[0].tool_name == "ha_call_service"


def test_policy_allow_list_cannot_be_silently_dropped(tmp_path: Path) -> None:
    ui = settings(tmp_path)
    ui._save_policy({"version": 0, "rule_effect": "allow", "rules": []})
    with pytest.raises(ValueError, match="rule_effect"):
        ui._save_policy({"version": 1, "rules": []})
    assert load_approval_policy(tmp_path).rule_effect == "allow"


def test_removing_pin_disables_event_decisions_in_same_write_lock(
    tmp_path: Path,
) -> None:
    ui = settings(tmp_path)
    ui._change_pin("POST", {"pin": "1234"})
    ui._save_policy({"version": 0, "event_decisions_enabled": True})
    ui._change_pin("DELETE", None)
    assert not load_approval_policy(tmp_path).event_decisions_enabled
    assert not (tmp_path / "approval_pin.json").exists()


def test_read_only_and_approval_master_take_effect_without_reload(
    tmp_path: Path,
) -> None:
    ui = settings(tmp_path)
    ui._save_features(
        {"flags": {"read_only_mode": True, "enable_tool_security_policies": True}}
    )
    policy = load_policy(tmp_path)
    assert policy.read_only and policy.security_policy is not None
    assert not policy.enabled("write", writes=True)


def test_backup_controls_share_canonical_backup_settings_file(tmp_path: Path) -> None:
    ui = settings(tmp_path)
    ui._save_backup_config({"enable_snapshot_actions": True, "backup_read_only": False})
    policy = load_policy(tmp_path)
    assert policy.flag("enable_snapshot_actions")
    assert not policy.flag("backup_read_only", True)
    assert (tmp_path / "backup_settings.json").exists()


def test_settings_page_uses_explicit_theme_without_global_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ha_mcp.settings_ui as shared

    def forbidden() -> None:
        raise AssertionError("Global theme storage must not be read")

    monkeypatch.setattr(shared, "_load_theme_prefs", forbidden)
    page = settings(tmp_path)._render(
        SimpleNamespace(cookies={}, headers={}, query_params={})
    )
    assert "__HA_MCP_THEME_PREFS__" not in page
    assert "<html" in page


def test_module_selection_and_instructions_reuse_existing_advanced_file(
    tmp_path: Path,
) -> None:
    ui = settings(tmp_path)
    ui._save_advanced(
        {
            "enabled_tool_modules": "tools_utility",
            "custom_instructions": "Keep user guidance.",
        }
    )
    policy = load_policy(tmp_path)
    assert policy.enabled("ha_get_logs", source_module="tools_logs")
    assert not policy.enabled("ha_call_service", source_module="tools_service")
    assert policy.custom_instructions == "Keep user guidance."
    assert (tmp_path / "feature_flags.json").exists()


def test_strict_bps_keeps_guide_available_and_rejects_conflicting_ui_edits(
    tmp_path: Path,
) -> None:
    ui = settings(tmp_path)
    ui._save_features({"flags": {"enable_strict_mandatory_bps": True}})
    with pytest.raises(ValueError, match="skill guide"):
        ui._save_tools({"states": {"ha_get_skill_guide": "disabled"}})
    policy = load_policy(tmp_path)
    assert policy.flag("enable_mandatory_bps")
    assert policy.enabled("ha_get_skill_guide", source_module="server")


def test_native_flags_honor_canonical_environment_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_mcp_tools.native_server_policy import NativePolicy

    monkeypatch.setenv("HAMCP_ENABLE_FILESYSTEM_TOOLS", "false")
    policy = NativePolicy(flags={"enable_filesystem_tools": True})
    assert not policy.flag("enable_filesystem_tools")


@pytest.mark.parametrize("reverse", [False, True])
def test_consolidated_retired_names_keep_restrictive_visibility(
    tmp_path: Path, reverse: bool
) -> None:
    states = [("ha_get_blueprint", "disabled"), ("ha_import_blueprint", "pinned")]
    exposure = [("ha_get_blueprint", False), ("ha_import_blueprint", True)]
    if reverse:
        states.reverse()
        exposure.reverse()
    (tmp_path / "tool_config.json").write_text(
        json.dumps({"tools": dict(states), "llm_api": dict(exposure)})
    )
    policy = load_policy(tmp_path)
    assert policy.tool_states["ha_manage_blueprints"] == "disabled"
    assert policy.llm_overrides["ha_manage_blueprints"] is False


def test_explicit_current_name_overrides_inherited_alias_setting(
    tmp_path: Path,
) -> None:
    (tmp_path / "tool_config.json").write_text(
        json.dumps(
            {
                "tools": {
                    "ha_get_blueprint": "disabled",
                    "ha_manage_blueprints": "enabled",
                }
            }
        )
    )
    assert load_policy(tmp_path).tool_states["ha_manage_blueprints"] == "enabled"


async def test_cancelled_ui_waiter_does_not_cancel_owned_file_io(
    tmp_path: Path,
) -> None:
    future = asyncio.get_running_loop().create_future()
    ui = settings(tmp_path)
    ui.manager.hass.async_add_executor_job = lambda *_args: future
    waiter = asyncio.create_task(ui._io(lambda: None))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not future.cancelled()
    stopping = asyncio.create_task(ui.async_stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    future.set_result(None)
    await stopping
