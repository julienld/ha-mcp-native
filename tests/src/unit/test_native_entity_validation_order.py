"""Invalid entity changes are rejected before a missing target can hide them."""

from typing import Any

import pytest

from ha_mcp.tool_api import CallContext

from .test_component_ws_search import _REAL_VOL
from .test_native_engine_backend import native as native
from .test_native_entity_writes import _setup


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"area_id": "missing_area"}, "VALIDATION_INVALID_PARAMETER"),
        ({"labels": ["missing_label"]}, "VALIDATION_INVALID_PARAMETER"),
        ({"expose_to": {"unknown_assistant": True}}, "VALIDATION_INVALID_PARAMETER"),
        ({"new_entity_id": "switch.changed"}, "VALIDATION_INVALID_PARAMETER"),
        ({"new_entity_id": "invalid format"}, "VALIDATION_INVALID_PARAMETER"),
        ({"name": "New name"}, "SERVICE_CALL_FAILED"),
        ({"new_entity_id": "sensor.changed"}, "SERVICE_CALL_FAILED"),
    ],
)
async def test_entity_preflight_precedes_target_lookup_without_writing(
    native: Any, monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any], code: str
) -> None:
    engine, manager, registry, devices, _, _, _ = _setup(native, monkeypatch)

    def validate_exposure(message: dict[str, Any]) -> dict[str, Any]:
        if message["assistants"] == ["unknown_assistant"]:
            raise _REAL_VOL.Invalid("Invalid assistant")
        return message

    manager.hass.data["websocket_api"]["homeassistant/expose_entity"] = (
        object(),
        validate_exposure,
    )
    result = await engine.call(
        "ha_set_entity", {"entity_id": "sensor.absent", **changes}, CallContext("actor")
    )

    assert result.is_error and result.status == "not_started"
    assert result.data["error"]["code"] == code
    assert registry.calls == [] and devices.calls == []
    manager.snapshots.registry_backup.assert_not_awaited()
