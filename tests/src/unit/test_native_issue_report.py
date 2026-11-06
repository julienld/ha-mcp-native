"""Native reports use Core observations, retained operation records and safe text."""

import asyncio
import importlib
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_engine_backend import native as native


@pytest.fixture
def reporter(native, tmp_path, monkeypatch):
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.issue_report"
    )
    from custom_components.ha_mcp_tools.native_server_policy import NativePolicy

    secret = "fixture_native_oauth_secret_12345"
    webhook = "fixture_native_webhook_capability_67890"
    target = tmp_path / "home-assistant.log"
    target.write_text(f"ERROR request /api/webhook/{webhook} failed; token={secret}\n")
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.const",
        SimpleNamespace(__version__="2026.10.2", KEY_DATA_LOGGING="log-path"),
    )
    monkeypatch.setattr(module, "detect_installed_version", lambda: "native-fixture")
    monkeypatch.setattr(
        module,
        "_detect_platform",
        lambda: {
            "os": "Fixture",
            "os_release": "1",
            "os_version": "1",
            "architecture": "fixture",
            "python_version": "3.14",
        },
    )
    manager = SimpleNamespace(
        hass=SimpleNamespace(
            config=SimpleNamespace(
                components=set(), location_name="Fixture Home", time_zone="UTC"
            ),
            data={"ha_mcp_tools": {}, "log-path": str(target)},
            states=SimpleNamespace(async_all=lambda: [object(), object()]),
            async_add_executor_job=lambda fn, *args: asyncio.ensure_future(
                asyncio.to_thread(fn, *args)
            ),
        ),
        entry=SimpleNamespace(
            data={"oauth_client_secret": secret, "webhook_id": webhook},
            options={"webhook_auth": "ha_auth"},
            state=SimpleNamespace(value="loaded"),
        ),
        settings_ui=SimpleNamespace(worker_id="fixture-owner"),
        policy=NativePolicy(),
        engine=SimpleNamespace(
            enabled_tools={"ha_get_state"},
            operations={
                "operation-fixture": {
                    "tool": "ha_get_state",
                    "status": "confirmed",
                    "writes": False,
                }
            },
        ),
        backend=SimpleNamespace(authorize=AsyncMock()),
        refresh_policy=AsyncMock(),
    )
    return (
        module.NativeIssueReport(manager, CallContext("owner")),
        manager,
        secret,
        webhook,
    )


@pytest.mark.parametrize("kind", ["runtime_bug", "agent_behavior", "feature_request"])
async def test_native_report_uses_actual_owners_and_scrubs_entry_credentials(
    reporter, kind
):
    report, manager, secret, webhook = reporter
    result = await report.report(
        report_type=kind,
        title="Fixture report",
        user_prompt=f"Inspect token={secret}",
        client_app="Fixture client 1",
    )
    serialized = json.dumps(result)
    assert result["success"] and secret not in serialized and webhook not in serialized
    assert result["diagnostic_info"]["home_assistant_version"] == "2026.10.2"
    assert result["diagnostic_info"]["entity_count"] == 2
    assert result["diagnostic_info"]["installation_method"] == "native"
    assert result["recent_logs"][0]["operation_id"] == "operation-fixture"
    assert result["recent_logs"][0]["execution_time_ms"] is None
    assert "timing unavailable" in result["formatted_report"]
    assert (
        result["issue_body"].splitlines()[-1]
        == "<!-- end of ha_report_issue report -->"
    )
    assert (
        result["issue_url"].startswith("https://github.com/")
        and "not published" in result["warnings"][-1]
    )
    assert manager.backend.authorize.await_count >= 2


async def test_native_report_withholds_results_when_authority_changes_during_render(
    reporter,
):
    report, manager, *_ = reporter
    original = manager.hass.async_add_executor_job

    async def revoke(function, *args):
        value = await original(function, *args)
        if function.__name__ == "build":
            manager.backend.authorize.side_effect = PermissionError("revoked")
        return value

    manager.hass.async_add_executor_job = revoke
    with pytest.raises(ToolFailure) as raised:
        await report.report(fields="diagnostic_info")
    assert raised.value.code == "AUTH_FORBIDDEN"


async def test_native_report_validates_fields_before_diagnostic_collection(reporter):
    report, manager, *_ = reporter
    with pytest.raises(ToolFailure):
        await report.report(fields={"invalid": True})
    manager.backend.authorize.assert_not_awaited()


async def test_native_report_preserves_known_secret_registry_minimum_length(
    reporter, monkeypatch
):
    report, *_ = reporter
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.issue_report"
    )
    monkeypatch.setattr(module, "known_secret_values", lambda: ("six123",))
    result = await report.report(user_comment="Observed value six123")
    assert "six123" not in json.dumps(result)
