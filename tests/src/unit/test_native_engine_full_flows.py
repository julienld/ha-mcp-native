"""Shared multi-step helper/options/subentry algorithms through Core owners."""

import copy
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, call

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.runtime.flows.session import FlowSession
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .test_component_helper_collections import FakeCollection, _hass
from .test_component_ws_search import FakeRegEntry, make_view
from .test_native_engine_backend import authorize
from .test_native_engine_backend import native as native
from .test_native_engine_flows import InvalidData
from .test_native_engine_flows import flows as flows
from .test_native_engine_helpers import helper_engine


async def test_revoked_actor_can_only_abort_its_own_flow_and_release_entry_lock(
    native: Any, flows: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, user, _, manager, backend, _ = _setup(native, monkeypatch)
    manager.async_init.return_value = {"flow_id": "owned", "type": "form"}
    credential = AsyncMock()
    actor = CallContext("actor", credential=credential)
    await backend.execute(
        "config.flow.start", {"kind": "options", "handler": "entry"}, actor
    )
    user.is_active = False
    credential.side_effect = PermissionError("revoked")
    with pytest.raises(ToolFailure, match="revoked") as rejected:
        await backend.execute(
            "config.flow.configure", {"flow_id": "owned", "user_input": {}}, actor
        )
    assert rejected.value.status == "not_started"
    manager.async_configure.assert_not_awaited()
    with pytest.raises(ToolFailure, match="does not own"):
        await backend.execute(
            "config.flow.abort", {"flow_id": "owned"}, CallContext("other")
        )
    await backend.execute("config.flow.abort", {"flow_id": "owned"}, actor)
    assert (
        not backend._flows.owned
        and not registry_update_lock("config_entry", "entry").locked()
    )


@pytest.mark.parametrize("kind", ["options", "subentry-create", "subentry-update"])
async def test_restore_session_preserves_expected_revision_without_recapturing(
    native: Any, flows: Any, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    _, _, entry, manager, backend, _ = _setup(native, monkeypatch)
    actor = CallContext("actor")
    session = FlowSession(
        backend, actor, expected_modified_at=entry.modified_at.timestamp()
    )
    entry.modified_at = datetime(2026, 10, 22, tzinfo=UTC)

    async def start() -> dict:
        if kind == "options":
            return await session.start_options_flow("entry")
        return await session.start_config_subentry_flow(
            "entry", "sensor", subentry_id="sub" if kind.endswith("update") else None
        )

    result = await Engine(backend).call_owned("restore", start, actor, writes=True)
    assert result.is_error and result.data["error"]["code"] == "CONFIG_CONFLICT"
    manager.async_init.assert_not_awaited()


async def test_native_core_rejection_uses_shared_field_error_and_redaction(
    native: Any, flows: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, _, manager, backend, engine = _setup(native, monkeypatch)
    backend.redaction_enabled = lambda: True
    manager.async_init.return_value = {
        "flow_id": "f",
        "type": "form",
        "step_id": "user",
        "data_schema": [
            {"name": "name"},
            {
                "name": "password",
                "selector": {"text": {"type": "password"}},
                "description": {"suggested_value": "private-test-value"},
            },
        ],
    }
    manager.async_configure.side_effect = InvalidData()
    result = await engine.call(
        "ha_config_set_helper",
        {
            "helper_type": "template",
            "config": {"name": "Example"},
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    assert result.is_error
    assert result.data.get("field_errors") == {"field": "invalid"}, result.data
    assert "private-test-value" not in str(result.data)
    assert "<redacted: set>" in str(result.data)
    manager.async_configure.assert_awaited_once()
    manager.async_abort.assert_called_once_with("f")


def _setup(
    native: Any, monkeypatch: pytest.MonkeyPatch, *, backup: Any = None
) -> tuple:
    from .test_native_engine_helpers import wsapi

    monkeypatch.setitem(
        sys.modules,
        "homeassistant.loader",
        SimpleNamespace(async_get_config_flows=AsyncMock(return_value={"template"})),
    )
    hass = _hass(FakeCollection())
    user = authorize(hass)
    modified = datetime(2026, 10, 20, tzinfo=UTC)
    entry = SimpleNamespace(
        entry_id="entry",
        domain="template",
        title="Example",
        modified_at=modified,
        subentries={},
    )
    entry.as_json_fragment = {
        "entry_id": "entry",
        "domain": "template",
        "title": "Example",
        "modified_at": modified.timestamp(),
    }
    manager = SimpleNamespace(
        async_init=AsyncMock(), async_configure=AsyncMock(), async_abort=Mock()
    )
    hass.config_entries = SimpleNamespace(
        flow=manager,
        options=manager,
        subentries=manager,
        async_entries=lambda domain: [],
        async_get_entry=lambda key: entry if key == "entry" else None,
    )
    view = make_view(
        entity={
            "sensor.example": FakeRegEntry(
                "sensor.example", config_entry_id="entry", platform="template"
            )
        }
    )
    monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: view)
    backend = native.NativeBackend(hass, helper_backup=backup)
    return hass, user, entry, manager, backend, helper_engine(backend)


async def test_native_menu_and_multiple_forms_share_the_legacy_walker(
    native: Any, flows: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, _, manager, backend, engine = _setup(native, monkeypatch)
    manager.async_init.return_value = {
        "flow_id": "f",
        "type": "menu",
        "menu_options": ["sensor"],
    }
    manager.async_configure.side_effect = [
        {
            "flow_id": "f",
            "type": "form",
            "step_id": "sensor",
            "data_schema": [{"name": "name"}, {"name": "state"}],
        },
        {
            "flow_id": "f",
            "type": "form",
            "step_id": "advanced",
            "data_schema": [{"name": "unit_of_measurement"}],
        },
        {
            "flow_id": "f",
            "type": "create_entry",
            "result": {"entry_id": "entry", "title": "Example"},
        },
    ]
    result = await engine.call(
        "ha_config_set_helper",
        {
            "helper_type": "template",
            "config": {
                "name": "Example",
                "next_step_id": "sensor",
                "state": "42",
                "unit_of_measurement": "W",
            },
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert manager.async_configure.await_args_list == [
        call("f", {"next_step_id": "sensor"}),
        call("f", {"name": "Example", "state": "42"}),
        call("f", {"unit_of_measurement": "W"}),
    ]
    assert result.data["entity_ids"] == ["sensor.example"] and not backend._flows.owned


@pytest.mark.parametrize("refusal", [None, "changed", "read_only", "revoked"])
async def test_native_options_snapshot_precedes_flow_and_omissions_keep_live_values(
    native: Any, flows: Any, monkeypatch: pytest.MonkeyPatch, refusal: str | None
) -> None:
    captured = []

    async def snapshot(payload: dict, context: CallContext) -> dict:
        captured.append(copy.deepcopy(payload))
        if refusal == "changed":
            entry.modified_at = datetime(2026, 10, 21, tzinfo=UTC)
        elif refusal == "read_only":
            engine.read_only = True
        elif refusal == "revoked":
            user.is_active = False
        return {"warnings": ["snapshot test warning"]}

    _, user, entry, manager, _, engine = _setup(native, monkeypatch, backup=snapshot)
    manager.async_init.return_value = {
        "flow_id": "f",
        "type": "form",
        "step_id": "init",
        "data_schema": [
            {
                "name": "state",
                "required": True,
                "description": {"suggested_value": "old"},
            },
            {"name": "unit_of_measurement", "description": {"suggested_value": "W"}},
        ],
    }
    manager.async_configure.return_value = {
        "flow_id": "f",
        "type": "create_entry",
        "result": {"title": "Example"},
    }
    result = await engine.call(
        "ha_config_set_helper",
        {
            "helper_type": "template",
            "helper_id": "entry",
            "config": {"state": "new"},
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    assert captured[0]["kind"] == "flow" and captured[0]["entry_id"] == "entry"
    if refusal:
        assert result.is_error
        manager.async_init.assert_not_awaited()
        manager.async_configure.assert_not_awaited()
    else:
        assert not result.is_error, result.data
        manager.async_configure.assert_awaited_once_with(
            "f", {"state": "new", "unit_of_measurement": "W"}
        )
        assert (
            result.data["updated"]
            and "snapshot test warning" in result.data["warnings"]
        )


@pytest.mark.parametrize("update", [False, True])
async def test_native_subentry_creation_and_reconfigure_keep_identity_and_values(
    native: Any, flows: Any, monkeypatch: pytest.MonkeyPatch, update: bool
) -> None:
    captured = []

    async def snapshot(payload: dict, context: CallContext) -> dict:
        captured.append(payload)
        return {}

    _, _, entry, manager, backend, engine = _setup(native, monkeypatch, backup=snapshot)
    row = SimpleNamespace(
        subentry_id="sub", subentry_type="sensor", title="Sub", unique_id=None
    )
    if update:
        entry.subentries["sub"] = row
    manager.async_init.return_value = {
        "flow_id": "f",
        "type": "form",
        "step_id": "user",
        "data_schema": [
            {"name": "name"},
            {"name": "unit", "description": {"suggested_value": "W"}},
        ],
    }

    async def submit(flow_id: str, data: dict) -> dict:
        entry.subentries["sub"] = row
        return (
            {"flow_id": "f", "type": "abort", "reason": "reconfigure_successful"}
            if update
            else {"flow_id": "f", "type": "create_entry"}
        )

    manager.async_configure.side_effect = submit
    result = await engine.call(
        "ha_config_set_helper",
        {
            "helper_type": "config_subentry",
            "entry_id": "entry",
            "subentry_type": "sensor",
            "config": {"name": "New"},
            "MandatoryBPS": False,
            **({"subentry_id": "sub"} if update else {}),
        },
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert result.data["subentry_id"] == "sub"
    assert result.data["operation"] == ("reconfigured" if update else "created")
    manager.async_configure.assert_awaited_once_with(
        "f", {"name": "New", **({"unit": "W"} if update else {})}
    )
    assert not backend._flows.owned
    assert bool(captured) is update
    if update:
        assert (
            captured[0]["kind"] == "subentry"
            and captured[0]["backup_id"] == "entry/sub"
        )
