"""Supervisor writes keep their owner, live admission and uncertain outcomes."""

import asyncio
import copy
import importlib
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_apps import apps as apps
from .test_native_engine_backend import native as native


class Options:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = copy.deepcopy(values)

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "Options":
        return cls(values)

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.values)


@pytest.fixture
def actions(apps: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    async def save(slug: str, model: Options) -> None:
        apps.replies[f"/addons/{slug}/info"].update(model.to_dict())

    apps.client.addons = SimpleNamespace(
        start_addon=AsyncMock(),
        stop_addon=AsyncMock(),
        restart_addon=AsyncMock(),
        uninstall_addon=AsyncMock(),
        rebuild_addon=AsyncMock(),
        addon_config_validate=AsyncMock(
            return_value=SimpleNamespace(valid=True, pwned=False)
        ),
        set_addon_options=AsyncMock(side_effect=save),
    )
    apps.client.store = SimpleNamespace(
        install_addon=AsyncMock(),
        update_addon=AsyncMock(),
        reload=AsyncMock(),
        add_repository=AsyncMock(),
        remove_repository=AsyncMock(),
    )
    monkeypatch.setitem(
        sys.modules,
        "aiohasupervisor.models.addons",
        SimpleNamespace(AddonsOptions=Options),
    )
    monkeypatch.setitem(
        sys.modules,
        "aiohasupervisor.models.store",
        SimpleNamespace(StoreAddRepository=SimpleNamespace),
    )
    apps.manager.visibility = SimpleNamespace(before=AsyncMock())
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.app_actions"
    )
    apps.service = module.AppActions(
        apps.manager, CallContext("actor"), {"action": "start"}
    )
    return apps


@pytest.mark.parametrize(
    "action,family,method",
    [
        ("install", "store", "install_addon"),
        ("update", "store", "update_addon"),
        ("restart", "addons", "restart_addon"),
        ("uninstall", "addons", "uninstall_addon"),
    ],
)
async def test_app_lifecycle_uses_existing_supervisor_owner(
    actions: Any, action: str, family: str, method: str
) -> None:
    result = await actions.service._execute_action_mode("core_fixture", action)
    assert result["success"]
    getattr(getattr(actions.client, family), method).assert_awaited_once_with(
        "core_fixture"
    )
    actions.raw.send_command.assert_not_awaited()


@pytest.mark.parametrize(
    "error", [ValueError("secret must not leak"), asyncio.CancelledError()]
)
async def test_provider_failure_after_dispatch_is_unknown_without_replay(
    actions: Any, error: BaseException
) -> None:
    actions.client.addons.start_addon.side_effect = error
    with pytest.raises(ToolFailure) as caught:
        await actions.service._execute_action_mode("core_fixture", "start")
    assert caught.value.status == "outcome_unknown"
    assert "secret must not leak" not in str(caught.value)
    actions.client.addons.start_addon.assert_awaited_once()


async def test_visibility_await_is_followed_by_live_authorization(actions: Any) -> None:
    async def revoke(*args: Any) -> None:
        actions.manager.backend.authorize.side_effect = PermissionError("revoked")

    actions.manager.visibility.before.side_effect = revoke
    with pytest.raises(ToolFailure, match="revoked") as caught:
        await actions.service._execute_action_mode("core_fixture", "start")
    assert caught.value.status == "not_started"
    actions.client.addons.start_addon.assert_not_awaited()


async def test_revocation_after_completed_app_operation_withholds_result_without_replay(
    actions: Any,
) -> None:
    async def start(slug: str) -> None:
        actions.manager.backend.authorize.side_effect = PermissionError("revoked")

    actions.client.addons.start_addon.side_effect = start
    with pytest.raises(ToolFailure) as caught:
        await actions.service._execute_action_mode("core_fixture", "start")
    assert caught.value.code == "AUTH_FORBIDDEN"
    assert caught.value.status == "confirmed" and caught.value.data["write_completed"]
    actions.client.addons.start_addon.assert_awaited_once()


async def test_app_options_use_provider_validator_and_preserve_future_keys(
    actions: Any,
) -> None:
    result = await actions.service._execute_config_mode(
        "core_fixture", {"options": {"future": {"enabled": True}}}
    )
    sent = actions.client.addons.set_addon_options.call_args.args[1].to_dict()[
        "options"
    ]
    assert sent == {"password": "fixture-secret", "future": {"enabled": True}}
    actions.client.addons.addon_config_validate.assert_awaited_once_with(
        "core_fixture", sent
    )
    assert result["status"] == "pending_restart" and result["post_write_verified"]


async def test_operator_list_keeps_previous_and_submitted_app_secrets_scrubbable(
    actions: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ha_mcp.runtime import redaction

    monkeypatch.setattr(redaction, "_known_secrets", set())
    actions.manager.policy.flags["redact_app_option_keys"] = (
        "core_fixture:backup_password"
    )
    row = actions.replies["/addons/core_fixture/info"]
    row["options"]["backup_password"] = "previous-native-scoped-secret"
    row["schema"].append({"name": "backup_password", "type": "string"})

    await actions.service._execute_config_mode(
        "core_fixture",
        {"options": {"backup_password": "submitted-native-scoped-secret"}},
    )

    assert {
        "previous-native-scoped-secret",
        "submitted-native-scoped-secret",
    } <= set(redaction.known_secret_values())


async def test_options_validation_cannot_overwrite_concurrent_changes(
    actions: Any,
) -> None:
    async def concurrent(*args: Any) -> Any:
        actions.replies["/addons/core_fixture/info"]["options"]["external"] = True
        return SimpleNamespace(valid=True)

    actions.client.addons.addon_config_validate.side_effect = concurrent
    with pytest.raises(ToolFailure) as caught:
        await actions.service._execute_config_mode(
            "core_fixture", {"options": {"future": False}}
        )
    assert caught.value.code == "CONFIG_CONFLICT"
    actions.client.addons.set_addon_options.assert_not_awaited()


async def test_rejected_options_never_reach_writer(actions: Any) -> None:
    actions.client.addons.addon_config_validate.return_value = SimpleNamespace(
        valid=False
    )
    with pytest.raises(ToolFailure) as caught:
        await actions.service._execute_config_mode(
            "core_fixture", {"options": {"future": False}}
        )
    assert caught.value.status == "not_started"
    actions.client.addons.set_addon_options.assert_not_awaited()


async def test_store_refresh_reports_actual_metadata_delta(actions: Any) -> None:
    async def reload() -> None:
        actions.replies["/store"]["addons"][0]["version_latest"] = "3.0"

    actions.client.store.reload.side_effect = reload
    result = await actions.service._execute_check_updates()
    assert result["changed"][0]["version_before"] == "2.0"
    assert result["changed"][0]["version_after"] == "3.0"
    actions.client.store.reload.assert_awaited_once()


async def test_repository_noop_is_proved_before_dispatch_not_inferred_from_error(
    actions: Any,
) -> None:
    result = await actions.service._execute_repository_action(
        "remove_repository", "absent"
    )
    assert result["success"]
    actions.client.store.remove_repository.assert_not_awaited()
    actions.client.store.remove_repository.side_effect = ValueError(
        "not registered after removal"
    )
    with pytest.raises(ToolFailure) as caught:
        await actions.service._execute_repository_action("remove_repository", "core")
    assert caught.value.status == "outcome_unknown"
    actions.client.store.remove_repository.assert_awaited_once()


async def test_double_cancel_keeps_app_lock_until_provider_finishes(
    actions: Any,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(slug: str) -> None:
        entered.set()
        await release.wait()

    actions.client.addons.start_addon.side_effect = held
    first = asyncio.create_task(
        actions.service._execute_action_mode("core_fixture", "start")
    )
    await asyncio.wait_for(entered.wait(), 1)
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    second = asyncio.create_task(
        actions.service._execute_action_mode("core_fixture", "stop")
    )
    try:
        for _ in range(8):
            await asyncio.sleep(0)
        assert not first.done()
        actions.client.addons.stop_addon.assert_not_awaited()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            _ = await first
        _ = await second
