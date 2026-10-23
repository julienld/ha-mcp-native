"""Entity registry diagnostics survive the conservative post-dispatch outcome."""

from typing import Any

import pytest

from ha_mcp.tool_api import CallContext

from .test_native_engine_backend import native as native
from .test_native_entity_writes import _setup

# Core 2026.10.2 EntityRegistry._async_update_entity uses this diagnostic for its
# own-area/own-name refusal. The adapter must not duplicate its private predicate.
_CORE_AREA_ERROR = (
    "An entity without a name of its own cannot have an area of its own, "
    "set the area on its device instead"
)


@pytest.mark.parametrize("effect_landed", [False, True])
async def test_core_area_diagnostic_does_not_claim_a_dispatched_write_never_started(
    native: Any, monkeypatch: pytest.MonkeyPatch, effect_landed: bool
) -> None:
    engine, _, registry, devices, _, _, _ = _setup(native, monkeypatch)
    entry = registry.rows["sensor.first"]
    entry.name = None
    entry.device_id = "main"
    update = registry.async_update_entity
    dispatches = []

    def core_update(entity_id: str, **changes: Any) -> Any:
        assert changes == {"area_id": "existing"}
        dispatches.append(entity_id)
        if effect_landed:
            # A listener can raise ValueError after saving. Even an identical
            # diagnostic must not be treated as evidence that no write landed.
            update(entity_id, **changes)
        raise ValueError(_CORE_AREA_ERROR)

    monkeypatch.setattr(registry, "async_update_entity", core_update)
    result = await engine.call(
        "ha_set_entity",
        {"entity_id": "sensor.first", "area_id": "existing"},
        CallContext("actor"),
    )

    assert result.is_error and result.status == "outcome_unknown"
    assert result.data["error"]["code"] == "OUTCOME_UNKNOWN"
    assert "inspect the registry before retrying" in result.data["error"]["message"]
    assert result.data["confirmed_phases"] == []
    assert dispatches == ["sensor.first"] and not devices.calls
    assert registry.rows["sensor.first"].area_id == (
        "existing" if effect_landed else None
    )
    assert result.data["core_error"] == _CORE_AREA_ERROR
