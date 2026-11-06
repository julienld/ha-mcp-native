"""Development component builds keep PEP 440 ordering at service gates."""

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.component_api import ComponentCaps
from ha_mcp.tools.tools_filesystem import (
    _COMPONENT_VERSION_CACHE,
    MIN_COMPONENT_VERSION,
    _assert_caps_version_ok,
    _fetch_caller_token,
    assert_extra_yaml_keys_supported,
)

from .test_caller_token_auth import _make_client_with_token
from .test_caller_token_auth import (
    _reset_token_cache_between_tests as _reset_token_cache_between_tests,
)


@pytest.mark.parametrize(
    "version", ["9.0.0.dev189467019982193", "9.0.0-beta", "v9.0.0"]
)
async def test_candidate_bootstrap_keeps_build_identity_and_enables_features(
    version: str,
) -> None:
    client = _make_client_with_token("gate-token", version=version)

    await assert_extra_yaml_keys_supported(client, ["alert2"])

    assert _COMPONENT_VERSION_CACHE[client] == version


async def test_development_build_of_required_stable_release_is_too_old() -> None:
    client = _make_client_with_token(
        "gate-token", version=f"{MIN_COMPONENT_VERSION}.dev1"
    )
    with pytest.raises(ToolError, match="too old"):
        await _fetch_caller_token(client)


def test_caps_gate_also_refuses_development_build_of_its_stable_floor() -> None:
    caps = ComponentCaps(
        schema_version=1,
        component_version=f"{MIN_COMPONENT_VERSION}.dev1",
        capabilities=frozenset(),
        limits={},
    )
    with pytest.raises(ToolError, match="too old"):
        _assert_caps_version_ok(caps)
