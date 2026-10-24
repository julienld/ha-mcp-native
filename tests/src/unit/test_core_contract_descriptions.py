"""Schema completeness must reflect strict serialization, including nested unions."""

import importlib.metadata
import importlib.util
import sys
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from .test_component_ws_search import _REAL_VOL as vol
from .test_component_ws_search import FakeHass


@pytest.fixture
def legacy_contract(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Use the installed codec with the same real Voluptuous as our schemas.

    Embedded tests install a facade without convert/UNSUPPORTED, and other
    modules replace Voluptuous during collection. Load this one-file codec
    under a private test name without replacing the facade its peers expect.
    """
    from custom_components.ha_mcp_tools import core_contract as module

    path = importlib.metadata.distribution("voluptuous-openapi").locate_file(
        "voluptuous_openapi/__init__.py"
    )
    spec = importlib.util.spec_from_file_location("_core_contract_legacy_codec", path)
    assert spec is not None and spec.loader is not None
    codec = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, codec)
    with monkeypatch.context() as imports:
        imports.setitem(sys.modules, "voluptuous", vol)
        spec.loader.exec_module(codec)
    monkeypatch.setattr(module, "_TO_JSON_SCHEMA", None)
    monkeypatch.setattr(module, "_OPENAPI_CONVERT", codec.convert)
    monkeypatch.setattr(module, "_OPENAPI_UNSUPPORTED", codec.UNSUPPORTED)
    monkeypatch.setattr(module, "_INVALID_ERRORS", (ValueError, vol.Invalid))
    return module


def test_legacy_description_preserves_live_union_fields_without_claiming_complete_validation(
    legacy_contract: ModuleType,
) -> None:
    module = legacy_contract

    branch = vol.Schema(
        {vol.Required("type"): "future", vol.Required("future_required"): str}
    )

    def union() -> Any:
        value_schemas = {"future": branch}
        default_schema = None

        def validator(value: Any) -> Any:
            selected = value_schemas.get(value["type"], default_schema)
            if selected is None:
                raise vol.Invalid("Unknown source type")
            return selected(value)

        validator.__module__ = "homeassistant.helpers.config_validation"
        validator.__qualname__ = "key_value_schemas.<locals>.key_value_validator"
        return validator

    def opaque_rule(value: Any) -> Any:
        if any(row["future_required"] == "refused" for row in value):
            raise vol.Invalid("The original Core rule rejects this combination")
        return value

    schema = vol.Schema(
        {
            vol.Required("id"): int,
            vol.Required("type"): "energy/save_prefs",
            vol.Required("sources"): vol.All([union()], opaque_rule),
        }
    )
    handler = Mock(side_effect=AssertionError("Description must not dispatch"))
    hass = FakeHass()
    hass.data["websocket_api"] = {"energy/save_prefs": (handler, schema)}
    result = module.describe_contract(hass, "energy/save_prefs")
    assert "future_required" in str(result.get("schema")), result
    branch_schema = result["schema"]["properties"]["sources"]["items"]["anyOf"][0]
    assert "future_required" in branch_schema["required"]
    assert result["description_complete"] is False and result["warnings"]
    for payload, valid in (
        ({"sources": [{"type": "future"}]}, False),
        ({"sources": [{"type": "future", "future_required": "refused"}]}, False),
        ({"sources": [{"type": "future", "future_required": "allowed"}]}, True),
    ):
        assert (
            module.validate_request(hass, "energy/save_prefs", payload)["valid"]
            is valid
        )
    handler.assert_not_called()


def test_legacy_history_description_keeps_fields_and_defaults_with_honest_warning(
    legacy_contract: ModuleType,
) -> None:
    module = legacy_contract

    schema = vol.Schema(
        {
            vol.Required("id"): int,
            vol.Required("type"): "history/history_during_period",
            vol.Required("start_time"): str,
            vol.Required("entity_ids"): [str],
            vol.Optional("include_start_time_state", default=True): bool,
        }
    )
    hass = FakeHass()
    hass.data["websocket_api"] = {"history/history_during_period": (Mock(), schema)}
    result = module.describe_contract(hass, "history/history_during_period")
    properties = result.get("schema", {}).get("properties", {})
    assert "entity_ids" in properties and "start_time" in properties, result
    assert properties["include_start_time_state"]["default"] is True
    assert result["description_complete"] is False and result["warnings"]


def test_successful_strict_description_has_no_loss_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_mcp_tools import core_contract as module

    schema = Mock()
    hass = FakeHass()
    hass.data["websocket_api"] = {"history/history_during_period": (Mock(), schema)}
    serializer = Mock(return_value={"type": "object"})
    monkeypatch.setattr(module, "_TO_JSON_SCHEMA", serializer)
    result = module.describe_contract(hass, "history/history_during_period")
    assert serializer.call_args.kwargs["strict"] is True
    assert result["description_complete"] is True
    assert "warnings" not in result


def test_nested_unsupported_validator_cannot_be_marked_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_mcp_tools import core_contract as module

    nested = object()

    def union(child: Any) -> Any:
        value_schemas = {"future": child}
        default_schema = None

        def validator(value: Any) -> Any:
            return value_schemas.get(value, default_schema)

        validator.__module__ = "homeassistant.helpers.config_validation"
        validator.__qualname__ = "key_value_schemas.<locals>.key_value_validator"
        return validator

    validator = union(union(nested))
    strict_attempts: list[bool] = []

    def serialize(node: Any, *, strict: bool = False, custom_serializer: Any) -> Any:
        if node is nested:
            strict_attempts.append(strict)
            if strict:
                raise ValueError("Future callable has no schema serializer")
            return {}
        return custom_serializer(node)

    monkeypatch.setattr(module, "_TO_JSON_SCHEMA", serialize)
    hass = FakeHass()
    hass.data["websocket_api"] = {"energy/save_prefs": (Mock(), validator)}
    result = module.describe_contract(hass, "energy/save_prefs")
    assert strict_attempts == [True, False]
    assert result["description_complete"] is False
    assert result["warnings"]


@pytest.mark.asyncio
async def test_info_advertises_contract_for_the_server_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_mcp_tools import websocket_api as wsapi
    from ha_mcp.tools import component_api, core_contract

    info = wsapi._do_info(FakeHass())
    ws = Mock(
        send_command=AsyncMock(
            side_effect=[
                {"success": True, "result": info},
                {"success": True, "result": {"status": "available", "schema": {}}},
            ]
        )
    )
    monkeypatch.setattr(
        component_api, "get_websocket_client", AsyncMock(return_value=ws)
    )
    monkeypatch.setattr(
        core_contract, "get_websocket_client", AsyncMock(return_value=ws)
    )
    result = await core_contract.core_contract(
        Mock(base_url="http://test", token="test", verify_ssl=True),
        "history/history_during_period",
    )
    assert result["status"] == "available"
    assert ws.send_command.call_args.args[0] == "ha_mcp_tools/core_contract"
