"""Configuration schema adaptation preserves the caller's real validator."""

from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock

import pytest

from .test_config_flow import cf, const


def test_runtime_schema_uses_its_input_schema_type_despite_peer_module_stubs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_mcp_tools import native_config

    schema = cf.vol.Schema(
        {
            cf.vol.Required(const.OPT_SERVER_PORT): int,
            cf.vol.Required(const.OPT_WEBHOOK_AUTH): str,
        }
    )
    monkeypatch.setitem(sys.modules, "voluptuous", MagicMock())
    importlib.reload(native_config)
    result = native_config.runtime_schema(schema, {})
    assert isinstance(result, type(schema))
    assert [marker.schema for marker in result.schema] == [
        const.OPT_WEBHOOK_AUTH,
        const.OPT_SERVER_PORT,
    ]


def test_native_schema_hides_only_legacy_connection_fields() -> None:
    from custom_components.ha_mcp_tools.native_config import runtime_schema

    schema = cf.vol.Schema(
        {
            cf.vol.Required(const.OPT_SERVER_PORT): int,
            cf.vol.Required(const.OPT_WEBHOOK_AUTH): str,
            cf.vol.Optional(const.DATA_NATIVE_OWNER_ID): str,
        }
    )
    result = runtime_schema(schema, {const.OPT_RUNTIME_MODE: const.RUNTIME_NATIVE})
    assert {marker.schema for marker in result.schema} == {
        const.OPT_WEBHOOK_AUTH,
        const.DATA_NATIVE_OWNER_ID,
    }
