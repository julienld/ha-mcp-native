"""Integration mode forwarding and the confirmation-before-backup contract."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.runtime.flows.walker import ReconfigureStatus
from ha_mcp.runtime.integration_reconfigure import ReconfigureRunner
from ha_mcp.runtime.reconfigure_identity import ReconfigureIdentity
from ha_mcp.runtime.reconfigure_preflight import PreparedReconfigure
from ha_mcp.tools.integration_reconfigure import (
    ReconfigureRunner as LegacyReconfigureRunner,
)
from ha_mcp.tools.tools_integrations import IntegrationTools


async def test_remote_integration_write_waits_for_restore_admission_even_without_backup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Lock()
    await gate.acquire()
    client = MagicMock(
        get_config_entry=AsyncMock(
            return_value={"entry_id": "entry", "domain": "demo"}
        ),
        send_websocket_message=AsyncMock(
            return_value={"success": True, "result": {"require_restart": False}}
        ),
    )
    monkeypatch.setattr(
        "ha_mcp.tools.tools_integrations.entry_write_guard", lambda *_args: gate
    )
    call = asyncio.create_task(
        IntegrationTools(client).ha_set_integration(entry_id="entry", enabled=False)
    )
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not call.done()
        client.send_websocket_message.assert_not_awaited()
    finally:
        gate.release()
    result = await asyncio.wait_for(call, 2)
    assert result["success"]
    client.send_websocket_message.assert_awaited_once()


class TestSetIntegrationModes:
    """Unit tests for ha_set_integration mode routing (issue #1814).

    The three modes — enable/disable, add (config flow), update options
    (options flow) — are mutually exclusive; the flow drivers themselves
    are covered by test_flow_error_parsing and the e2e suite, so these
    tests pin only the routing and parameter-validation layer.
    """

    @pytest.fixture
    def mock_client(self):
        client = MagicMock(get_config_entry=AsyncMock(return_value={"domain": "hue"}))
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": {"require_restart": False}}
        )
        client.start_config_flow = AsyncMock()
        client.start_options_flow = AsyncMock()
        return client

    @pytest.fixture
    def tools(self, mock_client):
        return IntegrationTools(mock_client)

    async def _assert_invalid(self, coro, fragment: str):
        with pytest.raises(ToolError) as exc_info:
            _ = await coro
        err = json.loads(str(exc_info.value))
        assert err["success"] is False
        assert err["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert fragment in err["error"]["message"], err["error"]["message"]

    # === Mutual exclusion ===

    async def test_domain_and_entry_id_rejected(self, tools, mock_client):
        await self._assert_invalid(
            tools.ha_set_integration(entry_id="abc", domain="hue"),
            "not both",
        )
        mock_client.start_config_flow.assert_not_called()
        mock_client.send_websocket_message.assert_not_called()

    async def test_enabled_and_domain_rejected(self, tools, mock_client):
        await self._assert_invalid(
            tools.ha_set_integration(domain="hue", enabled=True),
            "mutually exclusive",
        )
        mock_client.start_config_flow.assert_not_called()

    async def test_enabled_and_config_rejected(self, tools, mock_client):
        await self._assert_invalid(
            tools.ha_set_integration(entry_id="abc", enabled=True, config={"x": 1}),
            "mutually exclusive",
        )
        mock_client.send_websocket_message.assert_not_called()
        mock_client.start_options_flow.assert_not_called()

    # === Nothing-to-do guards ===

    async def test_no_arguments_rejected(self, tools):
        await self._assert_invalid(tools.ha_set_integration(), "Nothing to do")

    async def test_entry_id_alone_rejected(self, tools):
        await self._assert_invalid(
            tools.ha_set_integration(entry_id="abc"), "Nothing to do"
        )

    async def test_reconfigure_only_parameters_are_rejected_in_options_mode(
        self, tools
    ):
        """Identity guards and confirmation must not be silently ignored."""
        with (
            patch(
                "ha_mcp.runtime.integration_writes.update_config_entry_options",
                new=AsyncMock(return_value={"success": True}),
            ) as options_mock,
            pytest.raises(ToolError) as exc_info,
        ):
            await tools.ha_set_integration(
                entry_id="abc",
                config={"scan_interval": 30},
                expected_device_id="device-1",
            )

        err = json.loads(str(exc_info.value))
        assert err["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "reconfigure" in err["error"]["message"].lower()
        options_mock.assert_not_awaited()

    async def test_reconfigure_only_parameters_rejected_outside_reconfigure_mode(
        self, tools
    ):
        """The identity guards must not ride an unguarded options update."""
        with (
            patch(
                "ha_mcp.runtime.integration_writes.update_config_entry_options",
                new=AsyncMock(return_value={"success": True}),
            ) as options_mock,
            pytest.raises(ToolError) as exc_info,
        ):
            await tools.ha_set_integration(
                entry_id="abc",
                config={"scan_interval": 30},
                expected_unique_id="unique-1",
                confirm_token="sha256:whatever",
            )

        err = json.loads(str(exc_info.value))
        assert err["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "reconfigure" in err["error"]["message"].lower()
        assert err["rejected_parameters"] == ["confirm_token", "expected_unique_id"]
        options_mock.assert_not_awaited()

    async def test_set_integration_has_explicit_safety_annotations(self, tools):
        """The unified tool advertises its state-changing safety semantics."""
        annotations = tools.ha_set_integration.__fastmcp__.annotations
        assert annotations.open_world_hint is False
        assert annotations.destructive_hint is True
        assert annotations.read_only_hint is False
        assert annotations.idempotent_hint is False

    # === Reconfigure mode ===

    async def test_reconfigure_mode_delegates_to_shared_handler(
        self, tools, mock_client
    ):
        sentinel = {"success": True, "operation": "reconfigure"}
        with patch.object(
            ReconfigureRunner,
            "run",
            new=AsyncMock(return_value=sentinel),
        ) as run_mock:
            result = await tools.ha_set_integration(
                entry_id="abc",
                reconfigure=True,
                config={"host": "192.0.2.170"},
                expected_device_id="device-1",
                confirm_token="sha256:preview",
            )
        run_mock.assert_awaited_once_with(
            "abc",
            config={"host": "192.0.2.170"},
            expected_device_id="device-1",
            expected_unique_id=None,
            expected_mac=None,
            expected_entity_ids=None,
            confirm_token="sha256:preview",
        )
        assert result is sentinel

    @pytest.mark.parametrize("confirm_token", ["sha256:stale", "not-a-token"])
    async def test_reconfigure_rejects_untrusted_token_without_snapshot(
        self, tools, confirm_token
    ):
        """A token that does not match current state must not snapshot.

        The backup boundary sits behind token validation, so a rejected
        confirmation leaves no phantom `scope="edits"` entry burning the
        per-entity retention.
        """
        entry = {
            "entry_id": "abc",
            "domain": "shelly",
            "supports_reconfigure": True,
        }
        identity = ReconfigureIdentity()
        with (
            patch(
                "ha_mcp.runtime.integration_reconfigure.prepare_reconfigure_request",
                new=AsyncMock(
                    return_value=PreparedReconfigure(
                        entry_id="abc",
                        entry=entry,
                        flow_config={"host": "192.0.2.170"},
                        identity=identity,
                        expected_identity={
                            "device_id": None,
                            "unique_id": None,
                            "mac": None,
                            "entity_ids": [],
                        },
                    )
                ),
            ),
            patch(
                "ha_mcp.runtime.integration_reconfigure._reconfigure_preflight_token",
                return_value="sha256:current",
            ),
            patch(
                "ha_mcp.tools.auto_backup.get_global_settings",
                return_value=SimpleNamespace(enable_auto_backup=True),
            ),
            patch(
                "ha_mcp.tools.auto_backup._capture_pre_write_snapshot",
                new=AsyncMock(),
            ) as capture_mock,
            pytest.raises(ToolError) as exc_info,
        ):
            await tools.ha_set_integration(
                entry_id="abc",
                reconfigure=True,
                config={"host": "192.0.2.170"},
                confirm_token=confirm_token,
            )

        capture_mock.assert_not_awaited()
        payload = json.loads(str(exc_info.value))
        assert payload["status"] == ReconfigureStatus.STALE_PREFLIGHT
        # A stale token must force a fresh preflight, so the rejection must not
        # hand the caller a usable token to resubmit with.
        assert "confirm_token" not in payload
        assert "confirm_token" not in payload.get("context", {})

    async def test_reconfigure_mode_reuses_shared_preflight_validation(
        self, tools, mock_client
    ):
        entry = {
            "entry_id": "abc",
            "domain": "shelly",
            "title": "Kitchen",
            "supports_reconfigure": True,
        }
        identity = ReconfigureIdentity()
        with (
            patch(
                "ha_mcp.runtime.integration_reconfigure.prepare_reconfigure_request",
                new=AsyncMock(
                    return_value=PreparedReconfigure(
                        entry_id="abc",
                        entry=entry,
                        flow_config={"host": "192.0.2.170"},
                        identity=identity,
                        expected_identity={
                            "device_id": None,
                            "unique_id": None,
                            "mac": None,
                            "entity_ids": [],
                        },
                    )
                ),
            ) as prepare_mock,
        ):
            preview = await LegacyReconfigureRunner(mock_client).run(
                "abc",
                config={"host": "192.0.2.170"},
                expected_device_id="device-1",
            )

        assert preview["success"] is True
        assert "preview" not in preview  # status is the single signal
        assert preview["status"] == ReconfigureStatus.PREVIEW
        assert preview["confirm_token"].startswith("sha256:")
        session = prepare_mock.await_args.args[0]
        assert session.backend.client is mock_client
        prepare_mock.assert_awaited_once_with(
            session,
            "abc",
            config={"host": "192.0.2.170"},
            expected_identity={
                "device_id": "device-1",
                "unique_id": None,
                "mac": None,
                "entity_ids": [],
            },
        )

    async def test_reconfigure_mode_passes_prepared_identity_to_apply(
        self, tools, mock_client
    ):
        entry = {
            "entry_id": "abc",
            "domain": "shelly",
            "title": "Kitchen",
            "supports_reconfigure": True,
        }
        identity = ReconfigureIdentity(device_ids=["device-1"])
        with (
            patch(
                "ha_mcp.runtime.integration_reconfigure.prepare_reconfigure_request",
                new=AsyncMock(
                    return_value=PreparedReconfigure(
                        entry_id="abc",
                        entry=entry,
                        flow_config={"host": "192.0.2.170"},
                        identity=identity,
                        expected_identity={
                            "device_id": None,
                            "unique_id": None,
                            "mac": None,
                            "entity_ids": [],
                        },
                    )
                ),
            ),
            patch(
                "ha_mcp.runtime.integration_reconfigure.reconfigure_config_entry",
                new=AsyncMock(return_value={"success": True}),
            ) as apply_mock,
        ):
            preview = await LegacyReconfigureRunner(mock_client).run(
                "abc",
                config={"host": "192.0.2.170"},
                expected_device_id="device-1",
            )
            result = await LegacyReconfigureRunner(mock_client).run(
                "abc",
                config={"host": "192.0.2.170"},
                expected_device_id="device-1",
                confirm_token=preview["confirm_token"],
            )

        assert result == {"success": True}
        apply_mock.assert_awaited_once()
        assert apply_mock.await_args is not None
        assert apply_mock.await_args.kwargs["prepared"] == PreparedReconfigure(
            entry_id="abc",
            entry=entry,
            flow_config={"host": "192.0.2.170"},
            identity=identity,
            expected_identity={
                "device_id": None,
                "unique_id": None,
                "mac": None,
                "entity_ids": [],
            },
        )

    # === Mode delegation ===

    async def test_add_mode_delegates_to_create_config_entry(self, tools, mock_client):
        sentinel = {"success": True, "entry_id": "new123", "domain": "workday"}
        with patch(
            "ha_mcp.runtime.integration_writes.create_config_entry",
            new=AsyncMock(return_value=sentinel),
        ) as create_mock:
            result = await tools.ha_set_integration(
                domain="workday", config={"name": "Workday"}
            )
        session = create_mock.await_args.args[0]
        assert session.backend.client is mock_client
        create_mock.assert_awaited_once_with(session, "workday", {"name": "Workday"})
        assert result is sentinel

    async def test_add_mode_defaults_config_to_empty_dict(self, tools, mock_client):
        with patch(
            "ha_mcp.runtime.integration_writes.create_config_entry",
            new=AsyncMock(return_value={"success": True}),
        ) as create_mock:
            await tools.ha_set_integration(domain="sun")
        session = create_mock.await_args.args[0]
        assert session.backend.client is mock_client
        create_mock.assert_awaited_once_with(session, "sun", {})

    async def test_options_mode_delegates_to_update_config_entry_options(
        self, tools, mock_client
    ):
        sentinel = {"success": True, "entry_id": "abc", "updated": True}
        with patch(
            "ha_mcp.runtime.integration_writes.update_config_entry_options",
            new=AsyncMock(return_value=sentinel),
        ) as update_mock:
            result = await tools.ha_set_integration(
                entry_id="abc", config={"scan_interval": 30}
            )
        session = update_mock.await_args.args[0]
        assert session.backend.client is mock_client
        update_mock.assert_awaited_once_with(session, "abc", {"scan_interval": 30})
        assert result is sentinel

    async def test_enable_disable_mode_still_works(self, tools, mock_client):
        result = await tools.ha_set_integration(entry_id="abc", enabled=False)
        assert result["success"] is True
        assert result["entry_id"] == "abc"
        assert result["require_restart"] is False
        mock_client.send_websocket_message.assert_awaited_once_with(
            {
                "type": "config_entries/disable",
                "entry_id": "abc",
                "disabled_by": "user",
            }
        )

    async def test_enable_mode_sends_disabled_by_none(self, tools, mock_client):
        result = await tools.ha_set_integration(entry_id="abc", enabled=True)
        assert result["success"] is True
        mock_client.send_websocket_message.assert_awaited_once_with(
            {
                "type": "config_entries/disable",
                "entry_id": "abc",
                "disabled_by": None,
            }
        )
