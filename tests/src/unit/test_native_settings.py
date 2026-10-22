"""Native settings persist in HA's directory and preserve the same live gates."""

from __future__ import annotations

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


def test_tool_toggles_apply_to_native_policy_and_stay_in_instance_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(elsewhere))
    ui = settings(tmp_path / "ha")
    ui._save_tools({"states": {"ha_get_state": "disabled", "ha_list_services": "pinned"}, "llm_api": {"ha_list_services": False}})
    policy = load_policy(ui.directory)
    assert not policy.enabled("ha_get_state")
    assert "ha_list_services" in policy.pinned
    assert not policy.llm_overrides["ha_list_services"]
    assert not (elsewhere / "tool_config.json").exists()


def test_environment_pin_cannot_be_overwritten_and_is_not_persisted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISABLED_TOOLS", "ha_get_state")
    ui = settings(tmp_path)
    with pytest.raises(ValueError, match="Environment-pinned"):
        ui._save_tools({"states": {"ha_get_state": "enabled"}})
    assert not (tmp_path / "tool_config.json").exists()
    ui._save_tools({"states": {"ha_get_state": "disabled"}})
    raw = json.loads((tmp_path / "tool_config.json").read_text())
    assert "ha_get_state" not in raw["tools"]
    assert not load_policy(tmp_path).enabled("ha_get_state")


def test_policy_compare_and_swap_preserves_newer_policy(tmp_path: Path) -> None:
    ui = settings(tmp_path)
    result, _changed = ui._save_policy({"version": 0, "rules": [{"tool_name": "ha_call_service"}]})
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


def test_removing_pin_disables_event_decisions_in_same_write_lock(tmp_path: Path) -> None:
    ui = settings(tmp_path)
    ui._change_pin("POST", {"pin": "1234"})
    ui._save_policy({"version": 0, "event_decisions_enabled": True})
    ui._change_pin("DELETE", None)
    assert not load_approval_policy(tmp_path).event_decisions_enabled
    assert not (tmp_path / "approval_pin.json").exists()


def test_read_only_and_approval_master_take_effect_without_reload(tmp_path: Path) -> None:
    ui = settings(tmp_path)
    ui._save_features({"flags": {"read_only_mode": True, "enable_tool_security_policies": True}})
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


def test_settings_page_uses_explicit_theme_without_global_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ha_mcp.settings_ui as shared

    def forbidden() -> None:
        raise AssertionError("Global theme storage must not be read")

    monkeypatch.setattr(shared, "_load_theme_prefs", forbidden)
    page = settings(tmp_path)._render(SimpleNamespace(cookies={}, headers={}, query_params={}))
    assert "__HA_MCP_THEME_PREFS__" not in page
    assert "<html" in page
