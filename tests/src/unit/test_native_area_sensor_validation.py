"""Core sensor validation precedes area mutation, without hiding late failures."""

import copy
import sys
from typing import Any
from unittest.mock import Mock

import pytest

from ha_mcp.tool_api import CallContext

from .test_native_catalog_contract_area_writes import _serialize, _setup
from .test_native_engine_backend import native as native


@pytest.mark.parametrize(
    ("action", "kind"), [("create", "humidity"), ("update", "temperature")]
)
async def test_core_sensor_validator_refuses_before_area_registry_write(
    native: Any, monkeypatch: pytest.MonkeyPatch, action: str, kind: str
) -> None:
    engine, hass, areas, _, _ = _setup(native, monkeypatch)
    before = {key: _serialize(row) for key, row in areas.entries.items()}
    module = sys.modules["homeassistant.helpers.area_registry"]
    validator = Mock(
        side_effect=ValueError(f"Entity sensor.wrong is not a {kind} sensor")
    )
    monkeypatch.setattr(module, f"_validate_{kind}_entity", validator, raising=False)
    write = getattr(areas, f"async_{action}")

    def core_operation(**data: Any) -> Any:
        validator(hass, data[f"{kind}_entity_id"])
        return write(**data)

    operation = Mock(side_effect=core_operation)
    monkeypatch.setattr(areas, f"async_{action}", operation)
    arguments = {"kind": "area", "name": "Target", f"{kind}_entity_id": "sensor.wrong"}
    if action == "update":
        arguments["id"] = "existing"

    result = await engine.call("ha_set_area_or_floor", arguments, CallContext("actor"))

    assert result.is_error and result.status == "not_started", result.data
    assert result.data["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "sensor.wrong" in result.data["error"]["message"]
    validator.assert_called_once_with(hass, "sensor.wrong")
    operation.assert_not_called()
    assert {key: _serialize(row) for key, row in areas.entries.items()} == before


async def test_late_core_value_error_after_area_change_stays_unknown(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, hass, areas, _, _ = _setup(native, monkeypatch)
    module = sys.modules["homeassistant.helpers.area_registry"]
    validator = Mock()
    monkeypatch.setattr(
        module, "_validate_temperature_entity", validator, raising=False
    )

    def changed_then_failed(**data: Any) -> None:
        areas.calls.append(("update", copy.deepcopy(data)))
        areas.entries[data["area_id"]].name = "Stored"
        raise ValueError("A listener failed after storing the change")

    monkeypatch.setattr(areas, "async_update", changed_then_failed)
    result = await engine.call(
        "ha_set_area_or_floor",
        {
            "kind": "area",
            "id": "existing",
            "name": "Stored",
            "temperature_entity_id": "sensor.valid",
        },
        CallContext("actor"),
    )
    assert result.is_error and result.status == "outcome_unknown"
    assert result.data["error"]["code"] == "OUTCOME_UNKNOWN"
    assert areas.entries["existing"].name == "Stored" and len(areas.calls) == 1


async def test_unchanged_sensor_reference_keeps_cores_noop_validation_semantics(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _, areas, _, _ = _setup(native, monkeypatch)
    areas.entries["existing"].temperature_entity_id = "sensor.removed"
    validator = Mock(
        side_effect=AssertionError("Core does not revalidate an unchanged reference")
    )
    module = sys.modules["homeassistant.helpers.area_registry"]
    monkeypatch.setattr(
        module, "_validate_temperature_entity", validator, raising=False
    )
    result = await engine.call(
        "ha_set_area_or_floor",
        {
            "kind": "area",
            "id": "existing",
            "name": "Renamed",
            "temperature_entity_id": "sensor.removed",
        },
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert areas.entries["existing"].name == "Renamed"
    validator.assert_not_called()
